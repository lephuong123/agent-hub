# Văn phòng 3D (`/office`) — hiệu năng: vì sao "lag", đã sửa gì, đo thế nào

Chủ sản phẩm (2026-11-04): chế độ 3D "lag quá", cần một phase cho mượt. Máy đích là laptop dev (Intel Iris Xe, Windows, Chrome thật,
màn 60 Hz, 96 DPI → `devicePixelRatio` 1; người dùng có ~19 agent). Các lần đo trước chỉ ghi "29,9 FPS" — đó là con số của giới hạn
30 FPS trong `frame()`, không nói gì về khoảng dư hay khựng. Nhánh `perf/office-3d-smooth` (từ `110b4d9`, đã gồm PR #60).

## 1. Đo thế nào (lặp lại được)
- **Cờ gỡ lỗi** (`static/office/perf.js`, chỉ chạy khi có hash; không global/không query GPU khi tắt, không log, không chữ agent):
  - `#perf3d` → ghi từng khung: `interval` (khoảng giữa 2 khung VẼ — cái mắt thấy), `js` (thời gian trong `frame()` kể cả `renderer.render`),
    `gpu` (`EXT_disjoint_timer_query_webgl2`, Chrome/ANGLE D3D11 có), `calls`/`triangles`, khung dài > 50 ms kèm JS của khung trước
    (`longs`: JS cao → khựng do JS, ví dụ biên dịch shader; JS thấp → nghẽn GPU/trình duyệt). Đọc: `window.__office3dPerf.report()`, `reset()`.
  - `#uncap3d` → bỏ mọi giới hạn khung (vẽ MỌI rAF) để đo khoảng dư; kết hợp Chrome `--disable-gpu-vsync --disable-frame-rate-limit`.
  - `#debug3d` (có sẵn) giữ nguyên: `fps` trong `data-office3d` là số khung đã VẼ (giờ 60 khi động / 30 khi yên).
- **Hub giả** (không daemon, không `claude`): `mockhub.py <repo> <port> <n>` phục vụ static của worktree + `/office/state` (n agent, 3 team,
  approval, run) + `/sse/office` đẩy `tool.start/complete` ngẫu nhiên mỗi 1,5 s; `?h=22` giả giờ. Bench `bench.cjs` (playwright-core + Chrome
  thật, headless `--use-angle=d3d11 --enable-gpu` — headless vẫn ra `ANGLE (Intel Iris Xe … D3D11)`, rAF theo vsync 60, KHÔNG bị điều tiết khi cửa sổ
  mất focus như chạy headed trên máy đang dùng). Cửa sổ 1920×1080 (khung vẽ 1904×985), tier high. Kịch bản: toàn cảnh đứng yên 8 s; kéo
  xoay liên tục 7 s; bay tới 3 agent rồi Esc; đi bộ (tính cả lúc bấm 🚶: WASD 7 s + kéo xoay); theme tối; "cafe đông" (tua `performance.now`
  ×15 trong 45 s đầu để agent rảnh tới giờ nghỉ — rAF giữ thật). Có `PROFILE=1` (CDP `Profiler`) và một vòng thí nghiệm bật/tắt từng
  thành phần để đo giá GPU. Script nằm ở scratchpad phiên làm việc (không đưa vào repo — chỉ là công cụ đo).
- So sánh công bằng: baseline = `110b4d9` + đúng phần móc `perf.js` (vẫn giới hạn 30), chạy xen kẽ với bản mới trên cùng máy.

## 2. Nguyên nhân (có số)
1. **Giới hạn cứng 30 FPS** (`if (ts - lastDraw < 1000 / 30 - 3) return;`) — nguyên nhân chính của cảm giác "lag": mọi chuyển động camera
   (kéo xoay, damping, bay tới, đi bộ) chỉ 30 khung/s trên màn 60 Hz, thêm trễ tới 33 ms giữa chuột và hình. Máy KHÔNG thiếu sức: 20 agent
   JS 1,6–2 ms + GPU 9–11 ms/khung, 40 agent JS 2,5–4,7 ms + GPU 12–15 ms → vẽ 60 FPS được. Damping OrbitControls tính theo SỐ KHUNG
   (0,08/khung) → ở 30 FPS camera "trôi" chậm (hằng số thời gian ~0,4 s).
2. **Khựng 50–280 ms do biên dịch shader lần đầu** vật hiện ra: đèn trần (`setLampsVisible` khi camera < 26 m hoặc đi bộ), vật liệu mờ tường
   (`fadeMat`, chế độ đi bộ), thẻ nhân vật, vòng chọn, đạo cụ… Profile lúc vào đi bộ: `getProgramInfoLog` 172 ms; baseline 40 agent:
   kéo xoay 1 khung 284 ms (JS khung trước 285 ms), đi bộ 84 ms, bay tới 50 ms.
3. **GPU bị tam giác đè** (Iris Xe): 40 agent toàn cảnh 473 k tam giác. Thí nghiệm tắt từng phần (GPU p50 12,2 ms): bỏ chibi −5,3 ms
   (41 × 5,8 k = 228 k tam giác, mỗi chibi chỉ cao ~40–70 px ở toàn cảnh), bỏ batch World −5,6 ms (233 k), bỏ vật trong suốt −3,8 ms,
   tắt MSAA −3,6 ms, pixelRatio 0,75 −4,2 ms, tắt lấy mẫu bóng −2,3 ms.
4. **Không phải nguyên nhân** (đo rồi, để nguyên): `syncLabels` + declutter ~0,1 ms/khung; `actors.update` + `chibi.animate` < 0,5 ms;
   `setDaylight` mỗi giây chỉ bật `shadowDirty` khi đổi bậc 15 phút (không vẽ lại shadow map mỗi giây); `drawRuns` 1 lần/s (1 lần tải
   texture nhỏ); `drawCard` chỉ vẽ lại khi chữ đổi (`sig`); bone texture của mỗi SkinnedMesh tải lại mỗi khung (`texSubImage2D` ~0,15 ms
   tổng ở 41 chibi); phần còn lại của JS là duyệt cảnh của three (`renderBufferDirect`, `updateMatrixWorld`, uniform).
5. **Watchdog mong manh**: đếm khung ĐÃ VẼ, giới hạn 30 → rAF lệch nhịp (vd. 40 Hz khi máy bận) làm cổng 30 FPS bỏ 1/2 khung → 20 FPS
   "đo được" → tự hạ low dù máy vẽ được 40.

## 3. Đã sửa
- **Nhịp vẽ theo chuyển động** (`frameGate`, `util.js`): camera/cảnh đang động → vẽ theo màn hình (`MOTION_FPS` 60; màn 120/144 Hz vẽ cách
  1 khung → 60/72), đứng yên → `IDLE_FPS` 30 (nhân vật vẫn cử động như trước). "Động" = có `tween`, đang kéo (`down`), đi bộ có phím/đường đi,
  ma trận camera đổi so với khung trước (bắt cả damping, bám nhân vật, walker lerp), hoặc vừa có chuột/chạm/lăn/phím (`kick()`); giữ nhịp
  nhanh thêm `ACTIVE_HOLD` 400 ms. Cổng vẽ khi `ts − lastDraw ≥ chu kỳ − nửa khoảng rAF` → khớp lưới vsync (30 đều, không 25/50 lẫn lộn);
  rAF chậm hơn chu kỳ → vẽ mọi rAF. `#uncap3d` bỏ cổng.
- **Damping theo thời gian**: `dampingFactor = 1 − (1 − 0,08)^(dt·60)` (0,08 mỗi 1/60 s) — cùng cảm giác ở mọi FPS, hằng số thời gian ~0,2 s
  (trước ~0,4 s ở 30 FPS → bớt "trôi theo sau chuột").
- **Watchdog đếm nhịp rAF** (cả khung bỏ qua vì đứng yên), đặt TRƯỚC cổng nhịp vẽ: máy theo kịp → rAF = tần số màn hình; vẽ nặng → rAF
  chậm theo. Giữ nguyên mốc (< 24 hai cửa sổ 5 s → low; < 10 ba cửa sổ → fallback), luật bỏ khựng > 1 s / mất focus. Kiểm: CDP CPU ×6, 40 agent
  → tự hạ `low` sau ~10 s, không rơi vào fallback; máy thường không bao giờ hạ (mọi lần bench: `tier: high`).
- **Biên dịch shader trước** (`warmShaders()` cuối `applyState` khi World vừa dựng, kể cả sau `setQuality`): tạm bật mọi vật đang ẩn (trừ vật
  chứa đèn — đổi số đèn là sai biến thể), gán `fadeMat` cho một occluder, `renderer.compileAsync(scene, camera)` (KHR_parallel_shader_compile
  nếu có), rồi trả lại như cũ ngay (phần biên dịch chạy đồng bộ ở đầu `compileAsync`).
- **LOD nhân vật** (`chibi.js`): mỗi chibi dựng 2 geometry gộp từ cùng bộ chi tiết — gần (như cũ, ~5,8 k tam giác) và xa (số phân đoạn ×0,5,
  ~1,6–1,8 k); cùng xương, cùng vật liệu, cùng `boundingSphere` → không thêm draw call. `updateChibiLod(camera, viewH)` mỗi khung: chibi cao
  < 80 px trên màn hình → hình xa, > 96 px → hình gần (trễ giữa hai mốc). Toàn cảnh 20–40 agent = hình xa; bay tới / đi bộ gần = hình gần.
  So pixel ảnh chụp baseline ↔ mới (14:00, 1440×900): toàn cảnh lệch > 24/255 ở 0,12 % pixel, bay tới 0,72 % (pha hoạt hình), đi bộ 0,09 %.
- `perf.js` + `#perf3d`/`#uncap3d` như mục 1. Không đổi World API, tier, reduced-motion, context-loss, fallback < 10 FPS, CSP.

## 4. Trước / sau (Iris Xe, Chrome 1904×985, vsync 60, tier high; ms; "khung" = khoảng giữa 2 khung vẽ)
| Kịch bản | n | Trước: p50 / p95 / max · FPS · GPU p50/p95 | Sau: p50 / p95 / max · FPS · GPU p50/p95 |
|---|---|---|---|
| Toàn cảnh yên | 20 | 33,4 / 33,6 / 33,6 · 30 · 9,6/9,8 | 33,4 / 33,6 / 33,7 · 30 (chủ ý) · 8,8/9,0 |
| Kéo xoay | 20 | 33,4 / 33,6 / 33,8 · 30 · 9,9/10,7 | **16,7 / 16,8 / 16,9 · 60** · 8,9/9,7 |
| Bay tới agent | 20 | 33,4 / 33,6 / 34,0 · 30 · 11,1/12,9 | **16,7** / 33,4 / 33,6 · 51 (60 khi bay, 30 khi đứng nhìn) · 9,8/12,5 |
| Đi bộ (gồm lúc vào) | 20 | 33,4 / 33,6 / 34,5 · 30 · 11,4/14,2 | **16,7 / 16,9 / 33,5 · 59** · 9,2/11,3 |
| Theme tối, yên | 20 | 33,4 / 33,6 / 34,0 · 30 · 9,6/10,2 | 33,4 / 33,5 / 35,2 · 30 · 8,7/8,8 |
| Cafe đông, kéo xoay | 20 | 33,4 / 33,6 / 33,7 · 30 · 9,9/10,6 | **16,7 / 16,8 / 16,9 · 60** · 8,9/9,6 |
| Toàn cảnh yên | 40 | 33,4 / 33,5 / 33,6 · 30 · 12,7/13,1 | 33,4 / 33,5 / 33,6 · 30 · 10,4/12,1 |
| Kéo xoay | 40 | 33,4 / 33,5 / **283,9** (1 khung > 50) · 29 · 12,8/17,1 | **16,7 / 16,8 / 16,9 · 60** · 9,2/10,5 |
| Bay tới agent | 40 | 33,4 / 33,5 / 50,1 (1) · 30 · 15,3/16,9 | **16,7** / 33,4 / 33,6 (0) · 51 · 10,6/13,8 |
| Đi bộ (gồm lúc vào) | 40 | 33,4 / 33,5 / **83,5** (1) · 30 · 12,5/16,3 | **16,7 / 16,8 / 33,5 (0) · 59** · 12,2/14,1 |
| Theme tối, yên | 40 | 33,4 / 33,5 / 33,8 · 30 · 12,5/14,9 | 33,4 / 33,5 / 33,6 · 30 · 11,0/11,2 |
| Cafe đông, yên | 40 | 33,4 / 33,5 / 50,2 (1) · 30 · 12,6/14,7 | 33,4 / 33,5 / 33,6 (0) · 30 · 11,0/11,3 |

- Tam giác toàn cảnh: 20 agent 265 k → 182 k; 40 agent 473 k → 312 k (draw call không đổi: 175 / 253).
- JS/khung (p50): 1,7–3,4 ms (20 agent), 2,4–4,1 ms (40 agent) — trước/sau tương đương; không còn khung JS > 50 ms.
- Chạy headed (Chrome có cửa sổ, cùng máy) cho cùng kết quả: kéo xoay 16,7/16,8, đi bộ 16,7/16,8, không khung > 50 ms.
- Không giới hạn (`#uncap3d` + tắt vsync): số liệu nhiễu mạnh do iGPU đổi xung/nhiệt và hàng đợi GPU đầy (JS p95 ~30 ms là chờ GPU,
  không phải tính toán) — trung bình 70–137 FPS cả trước lẫn sau; dùng GPU p50/p95 ở bảng trên làm thước đo khoảng dư.

## 5. Rủi ro / còn lại
- Màn 120/144 Hz: chuyển động 60/72 FPS (không theo đủ tần số màn hình) — chủ ý để iGPU còn dư; đổi `MOTION_FPS` nếu cần.
- Đứng yên vẫn 30 FPS: nhân vật đi lại ở toàn cảnh mượt như trước (không hơn); chưa có "đứng hình hẳn" khi không gì động (nhân vật luôn thở).
- GPU còn lại chủ yếu là World (233 k tam giác ở 40 agent: hộp bo góc `seg 1` = 108 tam giác/hộp) + MSAA + vật trong suốt. 80 agent chưa
  đo lại; nếu cần: LOD/bỏ bo góc cho đồ nhỏ ở toàn cảnh, hoặc tắt MSAA ở tier low.
- Biên dịch shader trước vẫn tốn thời gian một lần lúc tải/dựng lại World (trước đây trả dần lúc dùng); máy không có KHR_parallel_shader_compile
  sẽ khựng một lần lúc dựng.
- LOD đổi hình ở ~80–96 px: đã so ảnh, nhưng chưa có người nhìn bằng mắt ở mọi góc (ví dụ đi bộ nhìn chibi xa 20–25 m).
- Đo trên hub giả; máy thật đang chạy 19 tiến trình `claude` có thể tranh CPU — watchdog mới vẫn tự hạ low nếu rAF < 24.
