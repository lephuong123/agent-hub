# Văn phòng 3D — chế độ "Vào văn phòng" (đi bộ)

> **Tiếp theo**: `office-coordinator.md` — người dùng điều khiển Điều phối viên (xuất hiện cạnh bàn của mình, thay avatar "Bạn · chủ hub"),
> [E] ngồi bàn ra lệnh, [E] gần agent để nói chuyện (steer / run_continue / run_start).

Chủ hub (2026-11-03): người dùng **bước vào** văn phòng `/office` như một văn phòng thật thay vì chỉ nhìn từ trên xuống kiểu nhà búp bê.
Làm trên nhánh cafe (`feat/office-cafe`, PR #52). Chỉ đổi client + template (`office.html`, `office.css`, `static/office.js`,
`static/office/{walk,world,actors}.js`); không đổi server/RPC/CSP, không asset ngoài, không thư viện mới (chỉ three 0.169.0 đã vendor;
KHÔNG dùng PointerLockControls — xoay bằng kéo chuột cho gọn, không cần vendor thêm).

## Giai đoạn
- **Đã xong (giai đoạn 1 — lõi)**: nút chuyển, avatar + camera 3/1, di chuyển + va chạm + bấm sàn tự đi, qua cửa vào mọi phòng, làm mờ tường
  che camera, tương tác gần (thẻ agent, agent chào, [E] chat/bảng/duyệt/trả lời/thoát), ánh sáng theo giờ thật + đèn trần + cây,
  reduced-motion, kiểm bố cục headless thành test.
- **Chưa làm**: âm thanh môi trường (tuỳ chọn, mặc định tắt), joystick ảo (đã chọn "chạm để đi" thay thế), pointer lock, va chạm
  camera với đồ đạc (chỉ làm mờ tường/vách), nhớ chế độ đi bộ qua lần tải trang.

## Điều khiển
- Nút **🚶** (`#office-walk`, cạnh nút "2D"; `aria-pressed`) bật/tắt chế độ đi bộ; nút **👁** (`#office-pov`, chỉ hiện khi đang đi bộ) hoặc
  phím **V** đổi góc nhìn thứ ba ↔ thứ nhất. Hai nút `hidden` trong template, office.js bỏ `hidden` khi có WebGL (fallback → ẩn lại).
  Đây là ngoại lệ thứ hai của quy ước "chỉ canvas + nút 2D" (chủ hub yêu cầu nút chuyển) — xem `office-3d.md`.
- **WASD / phím mũi tên** đi theo hướng camera (Shift chạy 4,2 m/s, thường 2,4 m/s); **kéo chuột / kéo tay** xoay hướng nhìn
  (`LOOK_SENS` 0,006 rad/px, dọc 2/3; vượt ngưỡng 5 px lấy mốc tại đó — `lookDrag`, không nhảy; chuột cộng dồn, áp một lần mỗi khung);
  **con lăn** đổi khoảng cách camera (1,8–7 m); **bấm / chạm sàn** → tự đi tới theo A* của `Grid` (bấm vào đồ đạc → dừng ở ô trống cuối).
  **E** tương tác; **Esc** đóng bảng/thẻ trước, lần sau ra toàn cảnh. Phím bị bỏ qua khi có bảng modal/chat mở, đang gõ chữ, hoặc có Ctrl/Alt/Meta.
- Mobile: chạm sàn để đi + kéo để xoay (chọn thay joystick ảo để không thêm phần tử DOM nhìn thấy).

## Động học (phase 2 — `office-3d-perf.md` §7)
- Avatar có **vận tốc**: `vel` tiến tới vận tốc mong muốn bằng `damp` (util.js, độc lập dt) — tăng tốc λ 14, hãm λ 24, kèm trần gia tốc
  28 m/s² (mỗi khung 60 Hz đổi tốc ≤ 0,47 m/s; dừng hẳn ~0,18–0,2 s sau khi nhả). `dt` kẹp 0,25 s, chia bước con ≤ 1/120 s → cùng quỹ đạo
  dù khung 16,7 hay 33,4 ms. Mỗi bước con: `circleMove` rồi `vel = quãng thực / h` (mất phần đâm vào tường). Yaw thân `dampAngle` λ 16.
- **Tự đi** (bấm sàn): đường A* kéo căng bằng `Grid.lineFreeR` (thân bán kính `WALK_R`), pure pursuit nhắm trước 0,6 m, bỏ đỉnh đã tới/đã
  vượt, hãm dần trong 0,8 m cuối; đi bằng cùng bộ vận tốc + va chạm như phím (không gán vị trí trực tiếp). Camera theo hướng đi (λ 2,5) trừ khi
  vừa kéo nhìn trong 1 s.
- **reduced-motion**: mọi λ = ∞ (vận tốc/camera gán thẳng), bấm sàn dịch chuyển tức thời, không bay vào.

## Camera
- Góc thứ ba = **spring-arm** (phase 2): hướng `camYaw`/`camPitch` damp λ 30 (λ 6 trong 0,6 s đầu khi bay vào), khoảng cách zoom λ 12; đầu avatar
  (pivot, y 1,0) gắn cứng — camera = pivot + d·len, luôn `lookAt(pivot)` (khoảng cách không còn co giãn 2,9–3,6 m, không trôi sau khi dừng).
  **Cần co theo lưới có chiều cao** (`armHit`: đi lưới bước 0,1 m dọc tia; ô bị chặn có `Grid.top` > độ cao tia − 0,1 → cần ngắn lại, ≥ 0,6 m;
  co ngay, dài lại λ 4); đồ cao sát sau lưng (cần < 1,2 m) → nâng cần dốc dần tới −1,3 rad thay vì dí camera vào đồ. Không raycast mesh.
  Vào đi bộ / đóng bảng / đứng dậy: lấy hướng + khoảng cách từ vị trí camera hiện tại → bay vào bằng chính damping cầu (không tween riêng).
  Trước phase 2: lerp vị trí 8/s.
  Tường/vách nằm giữa camera và avatar (raycast tới `World.occluders`: tường bắc/tây, vách phòng, kính, vách cafe) → gán tạm
  `World.fadeMat` (MỘT vật liệu riêng, mờ 0,18), trả vật liệu gốc khi hết che. Không đụng vật liệu bộ đệm `mat()`/`basic()`.
- Tường mỏng còn chen giữa camera và avatar vẫn làm mờ như dưới (raycast `occluders`, mảng/Set dùng lại — không cấp phát mỗi khung).
- Góc thứ nhất: mắt ở 1,02 m, ẩn avatar.
- Khi xem bảng / chat (camera bay tới như chế độ toàn cảnh) walker nhường camera; đóng lại → camera lướt từ chỗ đang đứng về sau lưng avatar.
- OrbitControls tắt trong chế độ đi bộ (kể cả `controls.update()`); ra khỏi chế độ → bay về toàn cảnh.

## Va chạm & đi đường (`office/walk.js`)
- Dùng lưới chướng ngại sẵn có của `World` (ô 0,5 m; tường đệm 0,15, đồ đạc 0,02–0,1; ghế không chặn). Avatar bán kính `WALK_R = 0,12`
  (nhỏ hơn đệm tường để qua cửa một ô lưới); `blockedAt` lấy mẫu tâm + 8 điểm. Phase 2: **`circleMove`** — va chạm hình tròn với AABB các ô
  bị chặn (bước con ≤ r/2, đẩy ra theo pháp tuyến tới điểm gần nhất, 2 vòng) → trượt dọc tường liền mạch (trước: thử cả bước rồi từng trục,
  tốc độ nhảy 2,4 → 0,57 → 2,05 m/s); `slideMove` giữ chữ ký, gọi `circleMove`. `Grid.top` (chiều cao vật chặn, tường 2,6; world.js truyền
  chiều cao bàn/ghế/sofa/tủ/quầy/cây) dùng cho cần camera. Bấm sàn: đi bằng bộ vận tốc + `circleMove` như phím.
- `World.rooms`: 8 phòng có tên + điểm `p` bên trong (server, thư viện, phòng họp, điều phối viên, cafe, pantry, phòng nghỉ, hàng ghế chờ);
  `roomAt(x, z)` → tên phòng trên dòng gợi ý; ngoài phòng = "Khu làm việc". Điểm vào của avatar: `spawnPoint(world)` = ô trống gần tâm khu
  làm việc nhất (giữa dãy phòng bắc và cafe, ngoài mọi phòng), nhìn về phía bắc. (Trước đây `(W − 1,4, D − 1,2)` sát cửa ra — xem "Sửa sau chạy thử".)

## Tương tác gần (office.js)
- Mỗi 0,2 s: agent gần nhất ≤ 1,8 m (giữ tới 2,6 m) → tự mở **thẻ thông tin** sẵn có (trạng thái, việc, tool cuối, run, approval + nút
  Duyệt/Trả lời/Chat vẽ trên thẻ) và agent **chào** (`Actor.greet`: bong bóng 👋; đang đứng thì quay mặt về người dùng 4 s, đang ngồi thì
  không xoay ghế). Đi xa → thẻ tự mở tự đóng (thẻ người dùng bấm chọn thì giữ). Chỉ là hiển thị, không đổi trạng thái agent.
- Dòng gợi ý (sprite gắn camera, chỉ `fillText`): "📍 <phòng> · [E] <hành động>" + phím tắt. **E** theo ưu tiên: đứng trước màn hình chat
  trong phòng điều phối viên (≤ 2,8 m) → `chat.open()`; gần bảng kanban / màn hình phòng server / bảng Cần bạn / HUD → `showBoard`;
  trước cửa ra → `flows.openLogout()` (xác nhận 3D); agent gần có approval → bảng duyệt, có câu hỏi → bảng trả lời, là điều phối viên → chat.
- `#office-panel` (sr-only, aria-live) đọc "Chế độ đi bộ… Bạn đang ở <phòng>. Bấm E: …".

## Sửa sau chạy thử trên trình duyệt (chủ hub, dữ liệu hub thật 19 agent) + 2 finding Medium của reviewer PR #54
- **Điểm xuất hiện sát cửa thoát**: camera dí vào cánh cửa, gợi ý đầu là "[E] Thoát", lùi một chút + bấm là bật "Thoát khỏi cfharness?".
  → `spawnPoint` giữa khu làm việc (sẽ đổi thành bàn điều phối viên ở task sau). Trong chế độ đi bộ cửa ra **không bấm được** (`pick` bỏ
  `exitMeshes` → bấm/chạm cửa = bấm sàn, tự đi tới); hộp thoại thoát chỉ mở khi bấm **E** chủ động trước cửa (chế độ toàn cảnh giữ nguyên).
- **Dòng gợi ý đè đầu hộp thoại / biển tên khu vực** (finding office.js:232): `hintShown(ui)` — ẩn khi có bảng modal, chat hoặc đang xem
  bảng (`walkBusy`); `hintLayout` gọn hơn (≤ 70 % ngang, ≤ 0,8 chiều cao) và sát **mép dưới** khung nhìn.
- **Tự chọn agent khi đang mở bảng/chat/modal** (finding office.js:302): `walkProximity` bỏ qua tự chọn/bỏ chọn khi `walkBusy(ui())`;
  `near` không đổi nên đóng bảng xong lần quét sau mới chọn.
- **Nhãn tên chồng thành đám (cafe, cụm đông)**: `syncLabels` mỗi khung chiếu nhãn ra màn hình rồi `declutter` (util.js, tham lam theo
  ưu tiên): luôn hiện agent đang chọn / đang rê chuột; rồi điều phối viên, agent có approval, gần camera, nhãn đang hiện (đỡ nhấp nháy).
  Đi bộ: chỉ nhãn agent trong 7 m. Nhãn bị ẩn vẫn có trong danh sách sr-only.
- Low: phím E/V trong chế độ đi bộ cũng `preventDefault` (focus đang ở nút 🚶/link); sửa tên test trong comment `office_layout_check.mjs`.

## An toàn
- Đi bộ chỉ là cách XEM: `walk.js` không gọi mạng, không đọc khoá phiên/CSRF, không dựng HTML (test tĩnh khoá lại). Mọi thao tác từ
  chế độ đi bộ gọi đúng hàm của chế độ toàn cảnh (`chat.open`, `flows.openApproval/openQuestion/openLogout`) → cùng `post_guard`, CSRF,
  khoá `k`, xác nhận + bước 2. Chữ của agent (tên, việc) chỉ vẽ bằng `fillText`.
- `prefers-reduced-motion`: bấm sàn → dịch chuyển tức thời; camera không trễ (snap); agent quay mặt tức thời.

## Cảm giác văn phòng thật (`world.js`)
- **Ánh sáng theo giờ thật** (giờ máy người dùng, cập nhật mỗi giây, áp cho cả chế độ toàn cảnh): bảng `DAYLIGHT` nội suy màu/cường độ
  trời + nắng, chụp đèn trần, màu kính cửa sổ: đêm (< 5h30, > 20h) xanh tối + đèn trần vàng; 7h30 sáng mát; trưa ấm như cũ; 18h30
  chiều cam; nắng đi từ đông sang tây. ESTOP vẫn ghi đè màu đỏ; tắt ESTOP trả về mức của giờ hiện tại (`hemiInt/dirInt` thay hằng 2,1).
- Đèn trần thả trên mỗi cụm bàn (≈ 1 đèn / 4 m) và dãy máy cafe (2 đèn/dãy), 2 cây góc đông khu làm việc. Kính cửa sổ + đèn trần
  dùng vật liệu riêng (`ownMats`, dispose cùng World).

## Kiểm tra
- `tests/office_walk_motion_check.mjs` (phase 2, `test_walk_motion_headless`): trượt tường 20°, qua mọi cửa, 10 000 bước ngẫu nhiên không lẹm;
  gia tốc ≤ 40 m/s² và dừng ≤ 0,25 s với dt đều / xen kẽ / gai; tự đi hãm dần, không lẹm; reduced dịch chuyển tức thời; camera đi thẳng/xoay
  6 rad/s giữ 3,40 m, CV tốc độ ≤ 0,01 với dt xen kẽ; sát đồ cao không chui vào đồ; 120 khung không tạo Set mới.
- `tests/office_layout_check.mjs` (commit theo finding Low của reviewer PR #52) + `tests/test_office_walk.py` gọi nó: dựng `World` thật
  (canvas giả) với 1/5/19/40/80 agent, có/không team: số chỗ cafe + giữ chỗ, không chồng lấn, A* bàn → cafe/pantry/quầy/ghế chờ/cửa ra,
  **avatar từ điểm vào tới được cả 8 phòng**, đi thẳng bằng phím qua cửa 4 phòng phía bắc, không xuyên vách server|thư viện, vách trước
  phòng, tường bắc; **điểm xuất hiện** không bị chặn, ngoài phòng, cách cửa ra ≥ 4 m, gần giữa bề ngang. `tests/office_walk_ui_check.mjs`
  (hàm thuần): gợi ý ẩn khi modal/chat/bảng mở + nằm nửa dưới; không tự chọn khi bận; declutter. Test tĩnh: cửa ra không nằm trong `pick`
  khi đi bộ, `walkProximity` gác `walkBusy`, `hint.sprite.visible` chỉ đặt qua `hintShown`. Thêm `node --check` mọi file JS office,
  test tĩnh "chỉ là cách xem", test template nút chuyển.
- **Chưa kiểm hình** bằng trình duyệt (run không có trình duyệt): cảm giác điều khiển, góc camera, độ mờ tường, màu theo giờ, FPS.

## Rủi ro còn lại
- Camera góc thứ ba có thể xuyên đồ đạc cao (tủ sách, rack) khi lùi sát — chỉ tường/vách được làm mờ.
- Cửa cafe/phòng rộng ~1 ô lưới trống: đi bằng phím phải nhắm tương đối thẳng (trượt theo trục giúp phần nào); bấm sàn luôn qua được.
- Thêm ~40 mesh đèn trần; raycast camera → ~60 occluder mỗi khung khi đi bộ. Chưa đo FPS trên máy yếu.
- Avatar vẫn đi được khi ESTOP (người dùng không phải agent); agent đứng im.
