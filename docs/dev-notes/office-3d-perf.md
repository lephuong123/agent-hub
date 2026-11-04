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
  chứa đèn — đổi số đèn là sai biến thể), gán `fadeMat` cho một occluder, `renderer.compile(scene, camera)` (KHR_parallel_shader_compile
  nếu có thì driver link song song), rồi trả lại như cũ ngay. Không dùng `compileAsync`: r169 thăm dò `currentProgram.isReady()` bằng
  setTimeout, vật liệu bị dispose giữa chừng (dựng lại World, `setQuality`, agent rời đi) → TypeError không bắt được (đã tái hiện).
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

---

## 6. Phase 2 (nhánh `perf/office-3d-smooth-2`, 2026-11-04): vì sao phase 1 "60 FPS" mà vẫn không mượt

Chủ sản phẩm: "phần 3D và mode walk vẫn chưa cho trải nghiệm mượt mà — đập đi làm lại cũng không sao". Phase 1 chỉ đo p50/p95 khoảng khung
trên hub giả ở máy rảnh và kết luận "60 FPS". Phase 2 đo **cảm giác**: đều nhịp (judder), khung lỡ, đổi nhịp 30↔60, vận tốc avatar nhảy,
camera trôi/co giãn — và đo cả khi CPU bị tranh (×2 ≈ laptop thật đang chạy ~19 tiến trình `claude`).

### 6.1 Cách đo (bench ngoài repo, scratchpad phiên làm việc)
- `bench.cjs` (playwright-core + Chrome thật, Iris Xe/ANGLE D3D11, 1904×985, DPR 1): tiêm bộ ghi trước khi trang tải (bọc rAF, đếm draw call,
  ghi chuột/phím, long task, heap) + vá `Scene.prototype.onBeforeRender` của three lúc phục vụ để biết khung nào THỰC SỰ vẽ, vị trí/hướng
  camera, có vẽ lại shadow map không, thời gian GPU (timer query). Không phụ thuộc `#perf3d`.
  Hợp đồng bench: cảnh vẫn `THREE.Scene` + `renderer.render`; camera góc thứ ba luôn `lookAt` đầu avatar y = 1,0 (vị trí avatar suy từ camera);
  id `#office-walk`, `#office-list button` giữ nguyên. `#debug3d` → bench đọc `window.__pacer()` (khoá, d, vsync).
- Kịch bản: `idle` 6 s, `orbit` kéo đều 5 s (chuột CDP ~120 Hz theo đồng hồ thật), `fly` bay tới agent và về, `walk_enter` bấm 🚶,
  `walk_w` giữ W 5 s, `walk_look` W + kéo 86°, `walk_wall` đi chéo vào vật cản 3 s, `stopgo` W 0,6 s / nghỉ 0,6 s ×5.
  CPU ×1 / ×2 (`Emulation.setCPUThrottlingRate`), n = 19 / 40, headless / headed.
- Chỉ số: **judder** = TB |iv_i − iv_{i−1}| trên các khung đang chuyển động (0 = đều; 16,7 = 60/30 xen kẽ), p99/max khoảng khung, số lần
  đổi nhịp 30↔60, **snap** = vận tốc avatar đổi > 0,5 m/s trong 1 khung, **trôi** = camera còn dời bao lâu sau khi nhả phím, `locks` của pacer.
- A/B: `main` (git archive) và nhánh chạy XEN KẼ, mỗi bên 2 lượt (số liệu dao động giữa các lượt), `--no-gpu-poll` (bỏ
  `getParameter(GPU_DISJOINT_EXT)` đồng bộ mỗi khung — chính bộ đo cũ làm nặng thêm lúc máy bận). GPU so bằng `gpuab.cjs` (2 trang cùng
  trình duyệt, timer query xen kẽ), độ sáng bằng `lum.cjs`, program key bằng `pkey.cjs`.
- Lệnh: `node bench.cjs --repo <A|B> --n 19,40 --throttle 1,2 --mode headless --no-gpu-poll --hash "#debug3d" --out X.json`;
  `node abrun.cjs <A> <B> <tag> 2 …` (xen kẽ); `node table.cjs A1.json B1.json … [--full]`; `node sumab.cjs A1 A2 -- B1 B2`.
- Hạn chế: bench đo thời điểm rAF của trang, không phải lúc hình lên màn (không thấy khung compositor/DWM bỏ, độ trễ input → photon);
  headless dùng vsync giả; CPU throttling chỉ làm chậm luồng JS của trang (không làm chậm GPU process, không mô phỏng iGPU chia TDP); hub giả
  không có agent vào/ra (khựng do dựng lại World không đo); đường đi `walk_look`/`walk_wall` phụ thuộc bố cục (chỉ so cùng n).

### 6.2 Nguyên nhân (baseline `main` 6f82fc5, có số)
1. **Nhịp khung khi CPU bị tranh** (×2): JS mỗi khung p95 21–31 ms (duyệt cảnh three) → rAF đến muộn xen kẽ → khoảng khung 16,7/33,4 lẫn lộn,
   đổi nhịp 33–108 lần / 5 s, judder 8–15 ms. `gapEst` (EMA khoảng rAF) trôi lên 25–33 ms khi máy bận. Đây là "giật" mắt thấy dù FPS TB ~50.
2. **Động học**: avatar không gia tốc/hãm (vận tốc 0 ↔ 2,4 m/s trong 1 khung, 11–12 snap mỗi stopgo); trượt tường đổi trục
   (2,4 → 0,57 → 2,05 m/s); camera lerp vị trí dt·8 → trôi 240–410 ms sau khi dừng, khoảng cách co giãn 2,9–3,6 m khi đi/xoay.
3. **Khựng một lần / định kỳ**: vào đi bộ 50–83 ms (×1) tới 133–267 ms (×2) — dựng nhân vật, vẽ gợi ý (measureText từng ký tự + font emoji),
   program bóng/bone texture lần đầu, shadow map quanh avatar; vẽ lại thẻ 640×560 có shadowBlur 13–32 ms (×2); việc mỗi giây dồn 1 khung.
4. **GPU ít dư khi đi bộ**: 10–12 ms p50 (p95 tới 15,4) — IBL (mẫu PMREM mỗi pixel), shadow map 2048 vẽ lại khi tâm bóng dời.
5. **Watchdog**: một lần GPU/driver đứng (callback rAF 27 s rồi 72 s, đo ở n=19 ×2 headed) → rAF 1 Hz → coi là "chậm" → tier low vĩnh viễn
   → fallback "slow", nút 🚶 ẩn.
6. **CPU của renderer**: 3 vật liệu trong suốt DoubleSide (vệt nắng, dải sáng sàn, vòng chọn) được three vẽ 2 lượt và đặt `needsUpdate` giữa
   2 lượt → tính lại program key (getParameters + getProgramCacheKey) 60–120 lần/s mỗi cái.

### 6.3 Đã làm lại / sửa (mỗi bước một commit)
- **Vòng vẽ → `office/pacer.js`** (module thuần, `tests/office_pacer_check.mjs`): vsync = phân vị 20 của 60 khoảng rAF (bỏ > 100 ms); d = 1 khi
  động (màn 120/144 Hz: 2), 2 khi yên; rAF < 50 Hz → vẽ mọi rAF. **Bộ điều tiết quá tải**: ≥ 4/30 khung động trễ (> 1,5·d·vsync, khung trước
  cũng ở nhịp động) → khoá đều 30 FPS 2 s; khoá lại trong 1,5 s sau khi nhả → ×2 (tối đa 8 s); sạch 10 s → 2 s. Gai > 4 vsync, cửa sổ
  `hitch(ms)` (bật/tắt đi bộ, dựng lại World, đổi chất lượng, tab hiện lại, khôi phục context) và lúc đứng yên không tính. dt lượng tử theo
  vsync. Hysteresis 150 ms thay `ACTIVE_HOLD` 400 ms. `onSkip(fn, key)`: việc phụ chạy trong rAF bị bỏ qua hoặc sau ≤ 250 ms.
  Ngưỡng 3/30 của kế hoạch đổi thành 4/30 (và bỏ khung đầu sau lúc đứng yên) vì A/B cho thấy khoá nhầm ở ×1 (n=40 walk_look).
- **Watchdog** (`watchdogStep`): khoảng > 1 s = đứng → bỏ cửa sổ, chờ 30 khoảng < 100 ms; chỉ cửa sổ rAF đều (p80 ≤ 1,5 × trung vị) mới tính
  `fps < 24` / `fps < 10`; `cfh_office_q=low` chỉ khi chậm đều. `checkShaderErrors` chỉ với `#debug3d`.
- **Đi bộ**: `circleMove` (va chạm hình tròn, trượt tường liền mạch), `Grid.top` (chiều cao vật), avatar có vận tốc (λ 14/24, trần 28 m/s²,
  bước con 1/120 s), tự đi pure pursuit, camera spring-arm (pivot cứng, hướng λ 30, cần co theo lưới, nâng cần khi đồ cao sát lưng), kéo nhìn
  không nhảy khi vượt ngưỡng (chi tiết: `office-walk.md`).
- **Vào đi bộ**: dựng sẵn khi rảnh (`requestIdleCallback`), `warmShaders` thêm `initTexture` + một lần vẽ thật vào ô 1×1 kèm pass bóng,
  gợi ý cắt chữ tìm nhị phân + emoji vẽ sẵn, khung đầu camera đứng yên + `hitch(500)`.
- **Thẻ**: nền (bóng đổ) vẽ một lần; trạng thái/nơi/tool tách ra dải 640×80 gắn TRÊN thẻ (kế hoạch ghi "dưới" — đặt trên để không đè
  nhân vật/nhãn ở màn dọc); nội dung ≤ 4 lần/s qua `pacer.onSkip`. Việc mỗi giây chia nhiều khung, DOM qua `setTimeout`.
- **GPU**: World không IBL ở tier high (hemi bù ×1,9, lệch độ sáng ≤ 1 %), kính `MeshBasicMaterial`; env map chỉ cho nhân vật.
  Shadow map 1024 cố định.
- **CPU renderer**: `forceSinglePass` cho các vật liệu trong suốt DoubleSide (program key tính lại: 60–120 lần/s → 0); LOD nhân vật mốc
  160/190 px khi đi bộ.

### 6.4 Trước / sau (A = `main` b9066db, B = nhánh; headless, `--no-gpu-poll`, 2 lượt xen kẽ mỗi bên; ô = khoảng giá trị qua các lượt)
Lưu ý: lần đo này máy rảnh hơn lúc lấy baseline đầu (và không còn gpu-poll), nên A ở ×2 cũng ít giật hơn §6.2; chỉ so trong cùng bảng.
"Đổi nhịp" của `stopgo` tăng vì B về 30 FPS ngay khi camera đứng yên (A giữ 60 thêm 400 ms) — lúc đó không có gì dời.

| n · CPU | kịch bản | FPS | khoảng khung p99 / max (ms) | judder (ms) | đổi nhịp | snap avatar | locks B | JS p95 |
|---|---|---|---|---|---|---|---|---|
| 19 · ×1 | orbit | 60 → 60 | 16,9 → 16,8–16,9 / 17 → 17–17,2 | 0,09–0,1 → 0,08–0,1 | 0 → 0 | – | 0 | 3,5–5,4 → 2,5–3,8 |
| 19 · ×1 | walk_enter | 54 → 50–51 | 33,5 → 33,5 / 33,5 → 33,6 | 0,31–0,34 → 0,32–0,49 | 2 → 2–4 | – | 0 | 2,7–3,5 → 2,2–3,6 |
| 19 · ×1 | walk_w | 59–60 → 60 | 16,9–17,3 → 16,9–17 / 16,9–33,4 → 17,2–17,6 | 0,08–0,35 → 0,09–0,14 | 0–4 → 0 | 2–4 → 0 | 0 | 4,5–5,9 → 2,2–3,2 |
| 19 · ×1 | walk_look | 60 → 60 | 16,9 → 16,9 / 17 → 16,9 | 0,09–0,1 → 0,08–0,1 | 0 → 0 | 7–9 → 2–3 | 0 | 4,5 → 1,9–2,4 |
| 19 · ×1 | walk_wall | 60 → 60 | 16,9 → 17 / 17,1 → 17,1 | 0,09–0,15 → 0,09–0,14 | 0 → 0 | 4 → 1–2 | 0 | 3–4 → 2,8–3 |
| 19 · ×1 | stopgo | 58–59 → 51–52 | 33,5 → 33,6 / 33,5 → 33,6 | 0,11–0,14 → 0,16–0,35 | 1 → 10 | 11–13 → 1–8 | 0 | 2,8–3,3 → 2–2,7 |
| 19 · ×2 | fly | 43–46 → 43 | 33,5–50,1 → 33,5 / 33,6–50,2 → 33,6–66,7 | 0,25–3,42 → 0,29–1,1 | 4–21 → 3–4 | – | 0 | 10,4–26,1 → 9,4–9,8 |
| 19 · ×2 | walk_enter | 47–50 → 49 | 33,5–66,8 → 33,5 / 83,5–100,2 → 33,5 | 2,18–2,26 → 0,33 | 1–4 → 2 | – | 0 | 11–15,1 → 8,2–9,2 |
| 19 · ×2 | walk_w | 60 → 60 | 16,9 → 16,9–17 / 16,9–17,6 → 16,9–17,2 | 0,09–0,11 → 0,11–0,12 | 0 → 0 | 4 → 0 | 0 | 10,8–13,7 → 7,9–9 |
| 19 · ×2 | walk_wall | 59–60 → 52–60 | 17–17,5 → 17,4–50,1 / 17,6–33,4 → 33,3–83,5 | 0,11–0,32 → 0,21–1,5 | 0–2 → 1–2 | 4 → 1 | 0–2 | 10,9–13 → 9,4–21,8 |
| 19 · ×2 | stopgo | 55–58 → 51 | 33,4–50,2 → 33,5 / 33,5–82,9 → 33,6 | 0,15–1,45 → 0,32–0,4 | 1–12 → 10 | 11 → 7–9 | 0 | 8,6–23,8 → 8,5–9,7 |
| 40 · ×1 | walk_w | 60 → 60 | 17 → 16,9–17 / 17,3–17,5 → 16,9–17,4 | 0,12–0,14 → 0,09–0,13 | 0 → 0 | 4 → 1 | 0 | 4,1–4,8 → 2,7–5,6 |
| 40 · ×1 | stopgo | 58–59 → 51–52 | 33,5 → 33,6 / 33,5–33,7 → 33,6–33,7 | 0,18–0,33 → 0,27 | 1–5 → 10 | 11 → 3–6 | 0 | 3,5–6,2 → 3,8–3,9 |
| 40 · ×2 | orbit | 55–60 → 48–59 | 16,9–33,5 → 33,4–33,5 / 18,7–49,9 → 33,5 | 0,12–2,87 → 0,49–0,53 | 0–43 → 6–8 | – | 0–1 | 15–23,7 → 17,7–18,1 |
| 40 · ×2 | walk_enter | 43–52 → 43–44 | 50,2–83,6 → 33,5–50 / 83,4–117 → 33,6–50,2 | 1,72–3,17 → 0,41–0,72 | 1–9 → 1–2 | – | 0 | 11,1–38,9 → 10,8–15,3 |
| 40 · ×2 | walk_wall | 59 → 60 | 33,3–33,4 → 16,9–17 / 33,3–33,5 → 16,9–17,1 | 0,49–0,5 → 0,09–0,11 | 4 → 0 | 1 → 2 | 0 | 16,4–19 → 10,1–11,5 |
| 40 · ×2 | stopgo | 58 → 51 | 33,5 / 33,5–50,1 → 33,6–33,8 | 0,15–0,58 → 0,38–0,47 | 1–8 → 10–14 | 11 → 7 | 0 | 14,5–16,9 → 8,8–12 |

Thêm (cùng lượt): camera trôi sau khi nhả W: n=19 248 → 67–81 ms, n=40 416 → 149–160 ms; stopgo trôi 400–413 → 142–166 ms; maxΔv avatar
stopgo 2,4 → 0,56–1,06 m/s, walk_wall 2,05–2,29 → 1,3–2,0. **Snap stopgo chưa đạt ≤ 1** (còn 1–9): avatar bị kẹp 28 m/s² (≤ 0,47 m/s mỗi
khung 60 Hz), phần còn lại là khung đầu sau khi đứng yên cách 33 ms (Δv ×2) — chỉ số "> 0,5 m/s/khung" không chia dt.

**Headed (Chrome có cửa sổ, vsync thật), n = 19:** ×1 mọi kịch bản chuyển động 60 FPS, p99 ≤ 17 ms, locks 0; GPU p50/p95 (timer của bench,
ms/khung) orbit 10,5/11,1 → 6,5/8,8, walk_look 9,8/12,4 → 8,6/9,9, walk_wall 11,0/12,7 → 10,7/11,9. ×2: vào đi bộ max 100,2 → 33,6 ms;
3 lượt B (1 có gpu-poll, 2 không) **không lượt nào sập / hạ tier / fallback** (baseline sập 2/3; lần A chạy lại hôm nay cũng không sập →
chưa tái hiện được nguyên nhân gốc, nhưng watchdog mới đã chặn đường "đứng một lần → hạ cấp vĩnh viễn", có test).
GPU cặp đôi (`gpuab.cjs`, trung vị): C9 (bỏ IBL, headed) đi bộ 8,91 → 6,88 ms, toàn cảnh 6,25 → 4,68; C10 (bóng 1024, ép vẽ lại bóng mỗi
lần render, headless) đi bộ 8,07 → 6,97, phần vẽ lại bóng +2,85 → +1,86 ms (mục tiêu +0,8 chưa đạt).

### 6.5 Ngân sách và các bước có điều kiện
- Ngân sách: JS khung đi bộ ≤ 4 ms ở ×1 (đo: p95 1,8–3,2 ms); GPU đi bộ ≤ 7 ms p50, ≤ 12 ms p95 trên Iris Xe.
- **C12 (bóng nướng sẵn)**: điều kiện "walk GPU p50 > 7 ms hoặc p95 > 12 ms (headed)". Sau C11: p95 9,9–11,9 (đạt), p50 8,6–10,7 ms theo timer
  của bench (gồm cả phần chờ trong callback rAF; `gpuab` đo riêng render: 6,9 ms). Điều kiện p50 thoả theo bench → **đáng làm nhưng chưa làm**
  trong phase này (để phase sau).
- **C13 (độ phân giải động)**: điều kiện GPU p95 > 14 ms ở ×1 headed — không thoả (≤ 11,9) → không làm.

### 6.6 Còn lại / rủi ro
- **Chưa kiểm trên máy thật có ~19 tiến trình `claude`** (checklist cảm giác của kế hoạch): bench chỉ mô phỏng bằng CPU throttling. Cần chủ sản
  phẩm mở `/office#debug3d` và ghi đạt/không đạt: xoay toàn cảnh 5 s (`__pacer().locks` tăng ≤ 1–2), bấm 🚶, giữ W 4 s rồi nhả, đi chéo vào tường,
  xoay chuột nhanh khi đi, đứng sát tủ cao xoay 360°, đứng yên 10 s, bấm sàn (quẹo thành cung, hãm khi tới), `prefers-reduced-motion`, mở 30 phút
  (tier vẫn high). Chưa có kết quả.
- Ảnh World không IBL (toàn cảnh 9h/14h/18h/tối đã so pixel) **cần chủ sản phẩm duyệt**; bị từ chối thì revert riêng commit C9.
- Dải trạng thái thẻ (trên thẻ) là bố cục mới — chưa chụp so sánh bằng mắt.
- Ô lưới 0,5 m dày hơn tường thật: sát tường, cần camera 0,6 m có thể nằm trong ô tường (vẫn làm mờ tường như trước).
- Pointer lock / lưu độ nhạy chuột: hoãn (xoay đã mượt hơn chính chuột đầu vào).
