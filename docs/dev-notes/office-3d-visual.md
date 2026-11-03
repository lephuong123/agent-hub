# Văn phòng 3D — nâng cấp hình ảnh (art direction + spec kỹ thuật)

Chủ sản phẩm (2026-11-03): "phần 3D thiết kế xấu quá — làm đẹp hơn và thực tế hơn". Nhánh `feat/office-3d-visual`.
Tài liệu này là hợp đồng cho HAI người làm song song (A "môi trường", B "nhân vật"). Không đổi hành vi, RPC, CSP, bố cục `Grid`.
Ràng buộc gốc vẫn giữ nguyên (xem `office-3d.md`, `office-walk.md`, `office-cafe.md`, `office-coordinator.md`): không CDN, không nới
CSP (không `blob:`, WASM, worker), không asset ngoài (GLB/ảnh) — mọi thứ thủ tục (hình khối + `CanvasTexture`), Three.js 0.169.0 vendored
(chỉ `three.module.min.js` + `OrbitControls.js`; KHÔNG có addon → mọi helper tự viết inline).

## 0. Chẩn đoán (vì sao đang xấu)
Đo bằng World thật trong Node (`n` = số agent): **n=30 → 1 543 mesh tĩnh + 31 chibi × ~23 mesh ≈ 2 250 draw call**, 192 vật liệu.
Vì quá nhiều draw call nên phải dùng `MeshLambertMaterial` rẻ nhất, không shadow → hệ quả nhìn thấy trong ảnh chụp:
1. **Phẳng, không chiều sâu**: Lambert + không tone mapping + không bóng/AO → đồ đạc "trôi" trên sàn, màu pastel bệt.
2. **Nhà trôi giữa khoảng trắng**: không có nền đất, không fog, mép sàn cắt cụt.
3. **Đồ chơi**: ghế = 2 hộp + đĩa, cây = 3 quả cầu, đèn thả = chấm đen to (nhìn từ trên che cả biển tên).
4. **Rối chữ**: biển phòng + biển team + nhãn tên + bong bóng đều là sprite lơ lửng cùng kiểu viên thuốc.
5. **Cafe** là một khối tím than khổng lồ, viền neon 1 px, lấn át cả cảnh.

→ Thứ tự ưu tiên: **gộp draw call trước** (mở đường cho vật liệu PBR + shadow), rồi ánh sáng/vật liệu, rồi hình khối, cuối cùng chi tiết.

## 1. Mục tiêu hình ảnh
**"Mô hình kiến trúc cắt lớp" (architectural cutaway diorama) của một văn phòng Bắc Âu ấm, với nhân vật như tượng vinyl (designer toy).**
- Ánh sáng mềm, bóng đổ rõ nhưng mịn, AO dưới chân đồ đạc; vật liệu thật: gỗ sồi có vân, vải dệt, kim loại sơn tĩnh điện, kính.
- Đỉnh tường/vách bị "cắt" tô màu than (`#3f3a35`, kiểu poché bản vẽ kiến trúc) → đọc ra ngay là mô hình cắt, sạch và sang.
- Nhà đặt trên một bệ (plinth) dày 0.3 m trên nền đất nhạt, tan dần vào nền trang bằng fog → không còn "trôi".
- Màu: trung tính ấm làm nền, màu team chỉ ở thảm/ghế/áo nhân vật (nhấn, không bệt). Neon cafe chỉ là điểm nhấn ban đêm.
- Ít chữ lơ lửng: tên phòng/team **gắn vào kiến trúc** (biển trên vách, chữ in trên thảm), chỉ tên agent còn là sprite.

### Bảng màu (sRGB hex)
| Vai trò | Sáng (light) | Tối (dark theme) |
|---|---|---|
| Nền trang / fog | `#faf9f5` (giữ) | `#262624` (giữ) |
| Nền đất ngoài nhà | `#ebe7df` | `#2c2b28` |
| Bệ nhà (mép plinth) | `#cfc6b8` | `#3a3732` |
| Sàn sồi (gốc texture) | `#c79c6e` | như sáng (nội thất không đổi theo theme, chỉ theo giờ) |
| Tường thạch cao | `#efe9df` | — |
| Vách thấp | `#e6dfd3` | — |
| Mặt cắt đỉnh tường/vách | `#3f3a35` | — |
| Chân tường (baseboard) | `#6d543c` (sồi sẫm) | — |
| Gỗ sáng (mặt bàn) | `#e6d6bd` | — |
| Gỗ óc chó (kệ, bàn điều phối viên) | `#6b4a33` | — |
| Kim loại sơn đen (chân bàn, khung) | `#26272b` | — |
| Nhôm (chân sao ghế, đèn) | `#a9aeb4` | — |
| Vải ghế (lưng) | `#34363c` | — |
| Lá cây | `#4f7d4a` / `#5f8f52` / `#3f6b3f` | — |
| Chậu | terracotta `#b9714f`, gốm trắng `#eeeae4` | — |
| Đèn ấm (emissive) | `#ffd9a0` | — |
Màu team (`TEAM_COLORS`) giữ nguyên làm khoá nhận diện, nhưng khi dùng cho bề mặt lớn thì **làm dịu**: `muted(c) = lerp(c → #8a8580, 0.25)`;
thảm team = `lerp(muted(c) → #f3eee6, 0.62)`.

## 2. Kiểm kê vật thể hiện có (world.js) — để A không sót
Sàn (`this.floor`, raycast) · tường bắc + tây cao + viền chân + gờ đỉnh · 4 ô cửa sổ cao + bậu (`skyMat`) · sàn 4 phòng bắc ·
**server**: 4 rack + mặt trước + 48 LED (`this.leds`, đổi vật liệu) + điều hoà · **thư viện**: 2 tủ sách + kệ + sách (`InstancedMesh`) +
bàn đọc tròn + 2 ghế bành · **phòng họp**: bàn + chân, 6 ghế, bảng trắng + vạch màu · **điều phối viên**: thảm, bàn, hộc, ghế giám đốc,
màn hình (`mgrSpot.screen`), 2 ghế khách, tủ hồ sơ, khung màn chat (`chatAnchor`), tranh, cây · vách thấp + kính (`_walls`, occluder) ·
**cụm team**: thảm 2 lớp, bàn (mặt, 2 hông, tấm chắn, bàn phím, chuột), màn hình (`seat.screen`), cây/cốc, ghế, biển team (sprite),
ESTOP team (`teamFx.ov`, `teamFx.lamp`) · **cafe**: sàn thảm, vách + viền neon (`rgbMats`), dải thảm dãy, bàn game (mặt, hông, viền RGB,
bàn phím + LED, màn hình riêng `cafeMats`, thùng máy + kính RGB, lon), ghế gaming, 2 arcade, quầy nước + ly, tủ lạnh kính, cây, biển ·
**dải nam**: pantry (sàn, quầy, máy cafe, hộp bánh, ly, tủ lạnh, bàn cao + 2 ghế quầy, bình nước, cây), phòng nghỉ (2 lớp thảm, 2 sofa,
bàn trà, ly, cây, đèn cây), hàng ghế chờ (sàn, 2 băng ghế), thảm lối vào, cửa "Thoát" (`exitMeshes`, `exitBoard`), bảng "Cần bạn" trên giá ·
**tường tây**: khung + bảng kanban, khung + màn server runs, đồng hồ (mặt `clockFace`, vành, 12 vạch, 3 kim) · **tường bắc**: khung HUD + 2 giá ·
đèn thả (`_lamps`, `lampMat`) trên cụm + cafe · cây góc · `hemi` + `dir` · `fadeMat`.

## 3. Renderer, môi trường, chất lượng (A — `office.js`, chỉ phần renderer/ánh sáng/tier)
Trong `main()` sau khi tạo renderer (~dòng 56–67):
```js
renderer.toneMapping = THREE.ACESFilmicToneMapping;   // AgX thử được nhưng làm pastel nhạt hơn — chọn ACES
renderer.toneMappingExposure = 1.05;                  // dark theme: 0.95
renderer.shadowMap.enabled = tier === 'high';
renderer.shadowMap.type = THREE.PCFShadowMap;         // PCF + shadow.radius (PCFSoft bỏ qua radius và đắt hơn)
renderer.shadowMap.autoUpdate = false;                // bóng TĨNH: chỉ vẽ lại khi world.shadowDirty
```
- **Bóng tĩnh**: trước `renderer.render` trong `frame()`: `if (world && world.shadowDirty) { renderer.shadowMap.needsUpdate = true; world.shadowDirty = false; }`.
  `World` bật `shadowDirty = true` khi: dựng xong, `setDaylight` đổi bậc mặt trời (lượng tử 0.25 giờ — ~50 lần/ngày), `setTheme`.
  Nhân vật KHÔNG đổ bóng vào shadow map (dùng bóng blob của B) nên map tĩnh luôn đúng.
- **Environment map tự viết** (`makeEnvironment(renderer)` ở `office/materials.js`): một scene nhỏ — hộp `BackSide` 20×8×20 màu `#5d5953`,
  sàn `#7a6650`, 4 tấm "softbox" `MeshBasicMaterial` màu `setScalar(5)` (HDR, PMREM dùng HalfFloat) trên trần, 1 tấm "cửa sổ" ấm
  `#ffe2b8 × 3` ở tường bắc → `new THREE.PMREMGenerator(renderer).fromScene(envScene, 0.04).texture`; dispose scene + generator ngay.
  `scene.environment = env; scene.environmentIntensity = 0.5` (có từ r163). **Không** dùng làm `scene.background`.
  `webglcontextrestored` → tạo lại env + `shadowMap.needsUpdate = true` (PMREM mất theo context).
- **Fog**: `scene.fog = new THREE.Fog(bg, near, far)` màu = `scene.background`. Toàn cảnh: trong `fitCamera()` lấy `dist` đã tính →
  `near = dist * 1.05, far = dist * 2.4`. Đi bộ / bay tới bảng: `near = 40, far = 140` (không mờ gì trong nhà). `applyTheme()` cập nhật màu fog
  + gọi `world.setTheme(dark)` + exposure.
- **Tier** `'high' | 'low'` (biến `tier` trong office.js, đồng bộ sang `materials.js` qua `setTier`):
  | | high | low |
  |---|---|---|
  | pixelRatio | `min(dpr, 1.5)` | `1` |
  | shadow map | 2048², PCF radius 2.5 | tắt |
  | env map | có | `scene.environment = null` |
  | vật liệu `surface()` | `MeshStandardMaterial` | `MeshLambertMaterial` (cùng map/vertexColors) |
  | AO decal, vũng đèn, glow neon | có | có (rẻ, giữ chiều sâu) |
  Chọn ban đầu: `sessionStorage.cfh_office_q === 'low'` → low; hoặc `(navigator.hardwareConcurrency || 8) <= 4 && devicePixelRatio >= 2` → low;
  còn lại high. **Hạ tự động**: trong khối đo FPS hiện có (cùng cửa sổ 5 s, cùng các luật bỏ qua khựng/mất focus), thêm
  `if (tier === 'high') lowWins = fps < 24 ? lowWins + 1 : 0; if (lowWins >= 2) setQuality('low')` — TRƯỚC luật `slowWins` (< 10 → fallback,
  giữ nguyên). Không tự nâng lại; ghi `sessionStorage.cfh_office_q = 'low'` (hết khi đóng tab). Reset `lowWins` ở `start()`.
  `setQuality(t)`: `setTier(t)`; pixelRatio; `renderer.shadowMap.enabled`; env; `wkey = ''; if (state) applyState(state)` (dựng lại World —
  đường dựng lại theo `layoutKey` sẵn có); `scene.traverse(o => { if (o.material) [].concat(o.material).forEach(m => m.needsUpdate = true); })`.
- **Gỡ lỗi cho QA**: nếu `location.hash` chứa `debug3d` → mỗi 2 s ghi `document.documentElement.dataset.office3d =
  JSON.stringify({ calls, triangles, fps, tier })` từ `renderer.info.render` (`renderer.info.autoReset` giữ mặc định). Không log, không chữ agent.
- `signStand` (office.js ~dòng 178): đổi sang `surface('wood')`/`surface('metal')` cho đồng bộ (tuỳ, không bắt buộc).

## 4. `office/materials.js` (MỚI, A sở hữu) — API chính xác
Chỉ import `../vendor/three-0.169.0/three.module.min.js`. Phải chạy được trong Node với canvas giả của `tests/office_*_check.mjs`:
**cấm** `getImageData/putImageData/createImageData/toDataURL/ctx.filter`, chỉ `fillRect/arc/ellipse/stroke/gradient/fillText`.
```js
export function setTier(t, opts)            // t: 'high'|'low'; opts.aniso (renderer.capabilities.getMaxAnisotropy()), mặc định 4. Xoá cache vật liệu.
export function getTier()                   // 'high' | 'low'
export function tex(kind)                   // CanvasTexture dùng chung, đã set colorSpace/wrap/anisotropy. kind ở §5.
export function surface(kind, opts)         // vật liệu dùng chung theo kind (+opts) — bảng §6. Luôn vertexColors:true, color trắng
                                            // (màu từng chi tiết nằm ở vertex color) → 1 vật liệu = 1 draw call cho cả văn phòng.
export function roundedBoxGeo(w, h, d, r, seg = 1)   // BufferGeometry tâm ở giữa, cache theo khoá làm tròn mm
export function mergeGeometries(parts)      // parts: [{ geo, matrix, color? }] → BufferGeometry non-indexed: position, normal, uv, color
export class Batch {                        // gom mesh tĩnh theo vật liệu
  add(geo, matrix, kind, color, opts)       // opts: { uv: 'box'|'keep' (mặc định 'box'), uvSize: mét/1 lần lặp texture, shadow: true }
  box(w, h, d, x, y0, z, kind, color, opts) // tâm đáy như util.box; opts.r > 0 → roundedBoxGeo; opts.rotY
  cyl(rt, rb, h, x, y0, z, kind, color, seg, opts)
  sphere(r, x, y, z, kind, color, seg, opts) // opts.scale [sx,sy,sz]
  build(parent)                             // mỗi kind → 1 Mesh (castShadow/receiveShadow theo §6), trả mảng Mesh; dispose được
}
export function makeEnvironment(renderer)   // §3, trả Texture PMREM
export function softTex(kind)               // 'ao' (hình chữ nhật mềm), 'pool' (vũng đèn), 'glow' (dải neon) — 64–128 px
```
**`roundedBoxGeo`** (thuật toán như addon RoundedBoxGeometry, viết lại):
```js
const n = seg * 2 + 1, hs = 0.5 / n, g = new THREE.BoxGeometry(1, 1, 1, n, n, n), p = g.attributes.position, q = g.attributes.normal;
// w, h, d = kích thước hộp; nv = hướng pháp tuyến
const v = new THREE.Vector3(), nv = new THREE.Vector3(); r = Math.min(r, w / 2 - 1e-4, h / 2 - 1e-4, d / 2 - 1e-4);
for (let i = 0; i < p.count; i++) {
  v.fromBufferAttribute(p, i);
  nv.set(v.x - Math.sign(v.x) * hs, v.y - Math.sign(v.y) * hs, v.z - Math.sign(v.z) * hs).normalize();
  p.setXYZ(i, Math.sign(v.x) * (w / 2 - r) + nv.x * r, Math.sign(v.y) * (h / 2 - r) + nv.y * r, Math.sign(v.z) * (d / 2 - r) + nv.z * r);
  q.setXYZ(i, nv.x, nv.y, nv.z);
}
// UV: để Batch tính lại bằng box-projection (opts.uv = 'box'); cache theo `${w|h|d|r|seg}` làm tròn mm.
```
(n lẻ → không đỉnh nào ở 0, dải giữa thành mặt phẳng). `seg=1` (108 tam giác/mặt hộp ~ 650/hộp) cho đồ nhỏ; `seg=2` chỉ cho vật "chính"
(mặt bàn, đệm sofa, quầy). Bán kính: mặt bàn 0.015, đệm 0.05–0.08, tủ 0.02, vách 0.01.
**UV `'box'`**: sau khi áp matrix, mỗi đỉnh lấy trục pháp tuyến lớn nhất: |nx| → (z, y), |ny| → (x, z), |nz| → (x, y), chia `uvSize` →
vân gỗ liền mạch giữa các chi tiết đã gộp, mật độ texture theo mét. `mergeGeometries`: `geo.toNonIndexed()` (nếu indexed) → `clone().applyMatrix4`
→ nối mảng; `color` (THREE.Color hoặc hex) ghi vào attribute `color` (linear: `new THREE.Color(hex)` đã convert).

## 5. Texture thủ tục (CanvasTexture, `tex(kind)`, tất cả POT, sRGB, mipmap, `RepeatWrapping`)
| kind | px | 1 lần lặp = | Công thức |
|---|---|---|---|
| `oakFloor` | 512 | 2 m | 8 hàng ván 64 px (25 cm); mỗi hàng cắt 2–3 tấm dài 0.8–1.4 m (lệch mối); mỗi tấm tint L ±6 %, hue ±2° quanh `#c79c6e`; 30–40 đường vân sóng `rgba(90,60,30,0.06–0.12)` lineWidth 1–2; khe dọc hàng 2 px `rgba(60,40,20,0.35)`, mối đầu 1 px; 1/12 tấm có mắt gỗ (ellipse radial gradient). Dùng `seeded(7)`. |
| `woodLight` | 256 | 1 m | `#e6d6bd`, vân thưa alpha 0.05 (laminate) |
| `walnut` | 256 | 1 m | `#6b4a33`, vân đậm alpha 0.15, tint ±8 % |
| `plaster` | 256 | 2 m | `#efe9df` + 3 000 đốm 1 px ±3 % L + 2 vệt radial alpha 0.03 |
| `carpet` | 256 | 1 m | nền TRẮNG (màu qua vertex color); 6 000 điểm 1 px sáng/tối alpha 0.08; mạch thảm tấm 50 cm alpha 0.08 |
| `fabric` | 128 | 0.25 m | nền trắng; dệt chéo (2 họ đường 45°, alpha 0.06) |
| `terrazzo` | 256 | 1 m | `#ebe5da` + 400 hạt 2–6 px (`#c9b8a6 #9aa39b #d9a088 #7d7a75`) |
| `tiles` | 256 | 1.2 m | sàn nâng phòng server: 2×2 tấm 60 cm `#c3c9cd`, khe 2 px `#9aa1a6`, 1/4 tấm đục lỗ (lưới chấm) |
| `sky` | 256×128 | 1 ô cửa | gradient dọc trắng → `#dfe7ea`, dải skyline mờ (chữ nhật alpha 0.10) — nhân với `skyMat.color` theo giờ |
| `screenCode` | 256×128 | 1 màn | nền đen, 14 dòng "code" (fillRect độ dài ngẫu nhiên, 3 màu nhạt) — nhân `screen.color` |
| `screenGame` | 256×128 | 1 màn | nền đen, đường chân trời + khối màu kiểu game |
| `softTex('ao')` | 64 | — | 10 hình chữ nhật bo đồng tâm, alpha cộng dồn 0.06 → mềm ở mép |
| `softTex('pool')` | 128 | — | radial `rgba(255,214,150,0.9)` → 0 |
| `softTex('glow')` | 64×16 | — | gradient dọc 0 → 1 → 0 |

## 6. Vật liệu theo lớp (`surface(kind)`; high = Standard, low = Lambert)
| kind | map | roughness | metalness | đổ/nhận bóng | dùng cho |
|---|---|---|---|---|---|
| `wood` | `woodLight` (uvSize 1) | 0.55 | 0 | ✓/✓ | mặt bàn, bàn họp, quầy, kệ sáng |
| `walnut` | `walnut` | 0.5 | 0 | ✓/✓ | tủ sách, bàn điều phối viên, khung bảng, cửa |
| `plaster` | `plaster` (uvSize 2) | 0.9 | 0 | ✓/✓ | tường, vách |
| `paint` | — | 0.7 | 0 | ✓/✓ | baseboard, mặt cắt đỉnh tường, tủ lạnh, điều hoà, rack |
| `fabric` | `fabric` (0.25) | 0.95 | 0 | ✓/✓ | ghế, sofa, ghế bành, băng ghế |
| `carpet` | `carpet` (1) | 1.0 | 0 | ✗/✓ | thảm team, phòng họp, cafe, phòng nghỉ (đặt `polygonOffset`) |
| `metal` | — | 0.4 | 0.6 | ✓/✓ | chân bàn đen, khung màn hình, chân đèn |
| `chrome` | — | 0.25 | 1.0 | ✓/✓ | chân sao ghế, tay nắm, vòi |
| `plastic` | — | 0.45 | 0 | ✓/✓ | bàn phím, chuột, thùng máy, ly, chậu gốm |
| `ceramic` | — | 0.35 | 0 | ✓/✓ | chậu, cốc, bồn |
| `plant` | — | 0.65 | 0 | ✓/✓ | lá (`side: DoubleSide` nếu dùng mặt phẳng) |
| `glass` | — | 0.05 | 0 | ✗/✗ | vách kính: `MeshPhysicalMaterial` high (`transmission` KHÔNG dùng — đắt), thay bằng `transparent, opacity 0.18, envMapIntensity 1.2`; low = Lambert opacity 0.22. **Không** gộp (occluder). |
| `floor` | `oakFloor` (repeat W/2, D/2) | 0.5 | 0 | ✗/✓ | `this.floor` (giữ là Mesh riêng) |
| `ground` | — | 1.0 | 0 | ✗/✓ | nền đất ngoài nhà |
| emissive | — | — | — | ✗/✗ | `MeshBasicMaterial({ toneMapped: false })`: màn hình, LED, neon, chụp đèn, kính cửa sổ (`skyMat` + map `sky`) |

## 7. Hình khối + gộp (A — `world.js`)
**Quy tắc gộp**: World có `this.batch = new Batch()`; mọi đồ tĩnh đi qua `batch.box/cyl/sphere/add`; cuối `_build()` gọi
`this.batchMeshes = this.batch.build(this.group)`. **KHÔNG gộp** (giữ Mesh riêng như cũ): `floor`, mọi phần tử của `occluders`
(fade đổi `.material` từng mesh — vẫn dùng `roundedBoxGeo` + `surface('plaster')` được), mặt màn hình (`seat.screen`, `cafe` screen, `mgrSpot.screen`),
`Board.mesh`, `exitMeshes`, `clockFace` + 3 kim, `teamFx.ov/lamp`, biển. LED rack → **một `InstancedMesh`** (48 instance, `setColorAt` trong `update()`
thay cho đổi vật liệu; reduced-motion = luôn sáng như cũ). Sách giữ `InstancedMesh` với vật liệu RIÊNG `MeshStandardMaterial({ roughness 0.7 })`
(low: Lambert) KHÔNG `vertexColors` (InstancedMesh dùng `instanceColor`), tự dispose như hiện tại.
Từng hạng mục (kích thước giữ nguyên chỗ chiếm sàn — `grid.rect` KHÔNG đổi):
- **Bệ + nền**: `floor` = Box(W, 0.3, D) đặt `y = -0.15` (mặt trên vẫn y = 0 cho raycast), `surface('floor')`; viền bệ = 4 dải `paint` `#cfc6b8`
  ngoài mép. Nền đất: `CircleGeometry(max(W, D) * 3, 48)` y = −0.3, `surface('ground')`, nhận bóng. `setTheme(dark)` đổi màu nền đất + viền bệ.
- **Tường**: tường bắc/tây `plaster` dày 0.25; **mặt cắt đỉnh** (cap) `paint #3f3a35` dày 0.02 rộng bằng tường; baseboard 0.1 cao `#6d543c`
  sát chân mọi tường + vách (cả hai mặt vách). Vách thấp: `roundedBoxGeo(r 0.01)`, cap than; khung kính: thanh nhôm `metal #a9aeb4` 0.04
  đứng mỗi 1.2 m + thanh trên.
- **Cửa sổ** (tường bắc, 4 ô/phòng → 2 ô/phòng): ô 0.9 × 0.7, đáy y = 1.75; kính = `skyMat` (map `sky`), khung 0.05 `metal #26272b`,
  1 song đứng + 1 song ngang, bậu `wood` 0.06. Ban ngày thêm "vệt nắng": quad dọc additive `softTex('glow')` màu `#fff2d8`, opacity
  `0.10 * daylight`, gộp tất cả vào 1 mesh/1 vật liệu `this.sunShaftMat`.
- **Bàn làm việc** (`_desk`): mặt `wood` roundedBox 1.3×0.035×0.78 r 0.012 seg 2; khung chữ T 2 bên `metal` (chân 0.04×0.6×0.06 + thanh đế
  0.06×0.03×0.7); tấm chắn trước bằng `fabric` muted team (mỏng 0.02, cao 0.3, trên mặt bàn — giống vách bàn văn phòng thật);
  máng cáp `metal` 1.0×0.05×0.12 dưới mặt bàn; bàn phím `plastic #e9e9e9` + chuột; giữ ngẫu nhiên cây nhỏ / cốc.
- **Màn hình** (`_monitor`): đế tròn `metal`, cổ 0.03 × 0.16, bezel roundedBox r 0.01 `metal #18191c`; mặt màn hình = `MeshBasicMaterial`
  riêng (như cũ, `toneMapped:false`, map `screenCode`), màu tắt `#1b1f26`, bật `#9fd8ff` (setScreen giữ đúng `.color.set`).
- **Ghế văn phòng** (`_chair`): đệm roundedBox 0.46×0.07×0.46 r 0.03 `fabric` muted team; lưng cong nhẹ (2 roundedBox xoay 0.12 rad) `fabric #34363c`;
  trụ `chrome`; **chân sao 5 cánh** (5 box 0.26×0.025×0.04 xoay 72°) + 5 bánh xe (sphere 0.025 `plastic #222`). ~13 phần — gộp nên không tốn draw call.
  Ghế gaming: như trên + tay vịn, sọc accent vẫn từ `CAFE_RGB` nhưng là `fabric`, không phải neon.
- **Rack server**: thân roundedBox `paint #2b2f36`, cửa lưới = 10 thanh ngang `metal` mảnh trên mặt trước, LED instanced. Sàn phòng server `tiles`.
- **Thư viện**: tủ `walnut` có 2 hông + nóc + đế + lưng tách (đọc ra là tủ, không phải khối); ghế bành `fabric` teal `#5b8a8c` với đệm bo r 0.06.
- **Phòng họp**: bàn `wood` bo, chân panel `metal`; thảm `carpet #8c8f93`; bảng trắng: khung nhôm + khay bút.
- **Điều phối viên**: bàn `walnut` + mặt da `fabric #3a3a40`; ghế lưng cao bo r 0.05; thảm `carpet #b5654a`; tranh = khung `walnut` + canvas
  `textCanvas` vẽ mảng màu trừu tượng (3 hình chữ nhật mềm).
- **Cây** (`_plant`): chậu `ceramic` hình côn (Cylinder 12 seg) + lớp đất `#4a3a2c`; tán = 7–12 "lá" là `SphereGeometry(r, 6, 4)` dẹt
  (scale 1 × 0.35 × 0.6) xoay ngẫu nhiên quanh thân theo `seeded`, 3 sắc lá; cây cao có thân `walnut`. Tất cả qua batch → 0 draw call thêm.
- **Sofa**: khung `fabric` sẫm, 3 đệm ngồi + 3 đệm lưng roundedBox r 0.07 seg 2 (khe 0.02 giữa đệm), chân gỗ 4 cái; gối tựa màu nhấn.
- **Cafe**: sàn `carpet #2a2638`; vách ngoài **sáng màu** `#e6dfd3` (cùng vách văn phòng) có dải neon lồng ở đỉnh (giảm cảm giác khối tím);
  bên trong mặt vách tối `#3a3350`. Viền neon: giữ 3 `rgbMats` (Basic, toneMapped:false) + dải glow additive `softTex('glow')` dày 0.25 dựng đứng
  trên mỗi viền — 3 vật liệu glow (1/màu), màu chép từ `rgbMats[i]` mỗi frame trong `update()`, opacity `0.25 + 0.35 * (1 - daylight)`.
  Màn hình game: map `screenGame`.
- **Đèn trần**: thay chụp đèn thả bằng **đèn LED thanh treo**: thân roundedBox 1.2×0.04×0.1 `metal #a9aeb4`, mặt dưới emissive `lampMat`
  (toneMapped:false), 2 dây cáp 0.006 tới y 3.4. Cả nhóm vào `this.lampGroup` (batch riêng con của group này). office.js:
  `world.setLampsVisible(walk.active || camDist < 26)` → toàn cảnh không còn chấm đen che chữ.
  **Vũng đèn ban đêm**: mỗi đèn một quad sàn 2.2 × 1.6 `softTex('pool')`, AdditiveBlending, depthWrite false, gộp 1 mesh, opacity
  `0.55 * (1 - daylight)`; cùng kiểu cho đèn cây phòng nghỉ và quầy cafe.
- **AO giả (contact shadow)**: mọi đồ đứng trên sàn (bàn, tủ, sofa, rack, quầy, ghế, chậu) thêm 1 quad `softTex('ao')` rộng hơn footprint 12 cm,
  y = 0.004, đen opacity 0.32 (ghế 0.2) — gộp **1 mesh / 1 vật liệu** `this.aoMat` (`transparent, depthWrite:false, polygonOffset -1`).
  Có ở cả hai tier (low thiếu shadow map vẫn "đặt" được đồ xuống sàn).
- **Biển**: bỏ sprite biển phòng/team/cafe/khu nam. Thay bằng:
  - phòng bắc: tấm biển 1.1×0.26 (`textCanvas` 512×128, chữ `#fffaf0` trên `#3f3a35`, chấm màu phòng) gắn **trên đầu vách trước** cạnh cửa,
    y = 1.08, quay +z; `MeshBasicMaterial({ toneMapped:false })`;
  - team: chữ tên team in **lên thảm team** ở mép bắc (`textCanvas` 512×96 trong suốt, chữ muted team đậm 70 % alpha) — quad sàn y 0.006;
    thêm cờ bàn nhỏ ở đầu cụm (cột `metal` 1.2 m + biển 0.5×0.16 hai mặt) để vẫn đọc được khi camera thấp;
  - cafe: biển neon (`textCanvas` chữ hồng phát sáng, nền trong suốt) gắn mặt ngoài vách bắc cafe; pantry/phòng nghỉ/ghế chờ: biển treo tường-đứng
    (giá đỡ) cao 1.6 m.
  Tất cả vẫn đẩy vào `this.signs` (để `setSignsVisible` ẩn khi xem bảng); không có `userData.base` → `setLabelScale` tự bỏ qua (đúng ý: biển
  là kiến trúc, không phóng to).
- **Đồng hồ / khung bảng / cửa**: khung `walnut` bo; mặt đồng hồ giữ MeshBasic (clockFlash đổi màu), kim `metal`.

## 8. Ánh sáng (A — `world.js` `_build` + `setDaylight` + `update`)
- `hemi` HemisphereLight (sky theo giờ, ground `#b89a7a`), `dir` = **key** (castShadow ở high), `fill` = DirectionalLight thứ hai không bóng,
  màu `#cfe0ff`, từ phía đối diện mặt trời (tây-bắc), cường độ theo bảng. Không thêm PointLight/SpotLight (mỗi đèn làm đắt MỌI fragment).
- Shadow `dir`: `shadow.mapSize = 2048²`, camera ortho `±0.62 * hypot(W, D)` quanh tâm, `near 1`, `far 3 * L`, `bias -0.0004`,
  `normalBias 0.03`, `radius 2.5`. Cập nhật `shadow.camera` khi dựng World (W, D đổi theo số agent).
- **Quỹ đạo mặt trời** (thay `dir.position.set(W*(0.9-0.8*sun), 20, D*0.95)`): `φ = π * clamp((h - 6) / 13, 0, 1)`, `L = max(W, D)`;
  `pos = (W/2 + 0.8 L cos φ, L (0.55 + 0.35 sin φ), D/2 + 0.6 L sin φ)` → sáng từ đông, trưa từ nam-cao, chiều từ tây; bóng đổ nghiêng thấy
  rõ từ camera toàn cảnh. Lượng tử `h` về bội 0.25 trước khi tính; đổi bậc → `shadowDirty = true`.
- **Bảng DAYLIGHT mới** (thay hằng `DAYLIGHT`, thêm 2 cột: cường độ fill, `scene.environmentIntensity` gợi ý — World đặt `this.envInt`,
  office.js chép sang `scene.environmentIntensity` mỗi giây cùng lúc gọi `setDaylight`):
  ```
  [h,    sky,       hemi, sun,       dir,  lamp,      glass,     day, fill, env ]
  [0,    '#8a9ad0', 0.55, '#9fb0ff', 0.30, '#ffd9a0', '#1c2540', 0,   0.15, 0.25]
  [5.5,  '#8a9ad0', 0.55, '#9fb0ff', 0.30, '#ffd9a0', '#1c2540', 0,   0.15, 0.25]
  [7.5,  '#eef3ff', 0.90, '#fff4e6', 2.40, '#f3ead8', '#cfe6f5', 1,   0.45, 0.50]
  [12,   '#fff8ee', 1.00, '#fff2dc', 2.80, '#f3ead8', '#d4ebf6', 1,   0.50, 0.55]
  [16.5, '#ffefdd', 0.95, '#ffd9ab', 2.60, '#f3ead8', '#e6e0cc', 1,   0.45, 0.50]
  [18.5, '#ffd2b8', 0.70, '#ff9a55', 1.80, '#ffd9a0', '#f29c62', 0.5, 0.35, 0.40]
  [20,   '#8a9ad0', 0.55, '#9fb0ff', 0.30, '#ffd9a0', '#1c2540', 0,   0.15, 0.25]
  [24,   '#8a9ad0', 0.55, '#9fb0ff', 0.30, '#ffd9a0', '#1c2540', 0,   0.15, 0.25]
  ```
  Đây là điểm xuất phát cho ACES exposure 1.05 — chỉnh ±20 % theo ảnh chụp, nhưng ban đêm PHẢI còn đọc được mặt nhân vật.
  `lampMat` ban ngày `#f3ead8` (tắt), đêm `#ffd9a0` (sáng; toneMapped:false nên rực). `daylight` điều khiển vũng đèn, vệt nắng, glow neon.
- **ESTOP**: giữ logic `update()` (hemi đỏ nhấp nháy, dir đỏ); thêm `fill.color` đỏ nhạt và trả lại trong `setEstop(false)`.
  Glow neon cafe đứng màu tĩnh khi ESTOP/reduced (như `office-cafe.md`).

## 9. Nhân vật (B — `chibi.js`, `actors.js` chỉ phần nhìn)
Phong cách: **tượng vinyl** — mịn, bóng nhẹ, tỉ lệ chibi giữ đầu to nhưng bớt "quả bóng".
- **Gộp theo khớp**: mỗi khớp chuyển động = 1 Mesh gộp nhiều chi tiết với vertex color: `legL` (ống + giày), `legR`, thân (torso + cổ áo +
  vest/cà vạt điều phối viên), `armL` (tay áo + bàn tay), `armR`, đầu (sọ + tóc + kiểu tóc + má hồng + miệng + lông mày), 2 mắt (nhóm
  `eyes[i]` giữ nguyên để chớp `scale.y`). Đạo cụ `cup/book/phone` giữ Mesh riêng (ẩn hầu hết thời gian). Mục tiêu **≤ 10 draw call / chibi**
  khi đứng thường (hiện ~23). B tự viết `mergeColored(parts)` trong chibi.js (≈ 25 dòng: `toNonIndexed` → `applyMatrix4` → nối + attribute
  `color`) — **không import `materials.js`** để hai người không chờ nhau. Hình gộp là riêng từng chibi (màu áo/tóc khác) → `dispose()` phải
  dispose các geometry này (geometry chung qua `geo()` thì không).
- **Vật liệu dùng chung** (tạo một lần ở module): `VINYL = MeshStandardMaterial({ vertexColors:true, roughness:0.6, metalness:0 })`,
  `HAIR = (… roughness 0.42)`, `EYE = (… color '#1e1714', roughness 0.15)`. Env map + hemi tạo viền sáng tự nhiên; **không** inverted-hull
  outline (gấp đôi draw call). Tuỳ chọn nếu kịp: rim nhẹ qua `onBeforeCompile` trên `VINYL` (`+= rimCol * pow(1 - dot(N, V), 3) * 0.2`).
  Sprite/ring/bóng vẫn `MeshBasic`.
- **Tỉ lệ/hình**: sọ `SphereGeometry(0.3, 28, 20)` scale (1.0, 0.93, 0.95); thân = `LatheGeometry` profile bo (vai 0.17 → eo 0.14 → hông 0.16,
  cao 0.3, 16 seg) thay capsule; tay capsule dài thêm 15 %, bàn tay hơi dẹt; giày roundedBox-like (sphere scale 1, 0.55, 1.4) đế sẫm hơn.
  Tóc: giữ 6 kiểu nhưng thêm lọn mái (2–3 sphere dẹt) để bớt "mũ bảo hiểm"; điều phối viên giữ vest + tai nghe.
- **Mặt**: mắt ellipse to hơn 10 %, tròng nâu sẫm + 2 điểm sáng (trắng, gộp vào mắt với vertex color); lông mày = box 0.07×0.014 màu tóc
  sẫm; má hồng = đĩa phẳng gộp vào đầu, màu `lerp(skin → #f39a9a, 0.4)`, KHÔNG transparent; miệng giữ torus nhỏ.
- **Quần áo**: áo = màu team làm dịu (`lerp(color → #8a8580, 0.2)`, ±L theo `shade()` hiện có) + cổ áo trắng (torus 0.12×0.02);
  quần `PANTS`; giày `#2d2622`.
- **Bóng blob**: `CanvasTexture` radial 64 px dùng chung (đen alpha 0.45 → 0), `MeshBasicMaterial({ transparent, depthWrite:false,
  polygonOffset:true, polygonOffsetFactor:-2, toneMapped:false })`; bán kính 0.34 đứng / 0.42 ngồi; khi đi bộ dao động scale ±5 %
  (không khi reduced). `castShadow = false` mọi mesh chibi; `receiveShadow = true` cho thân/đầu (đọc bóng tĩnh dưới bàn).
- Picking không đổi: `group.userData.actorId` ở gốc, mọi Mesh vẫn là con cháu của `group`.
- **Nhãn & bong bóng** (B sở hữu phần chữ trong `util.js`): `labelSprite` vẽ canvas 2× (640×144) cho nét, pill nền `rgba(255,253,248,0.94)`
  + bóng đổ canvas (`shadowBlur 10`, `rgba(0,0,0,0.18)`), chấm team có viền trắng; `SpriteMaterial({ toneMapped:false, fog:false })`.
  **Giữ hợp đồng**: trả `Sprite`, `userData.base = [1.6k, 0.36k]`, `renderOrder 5`, `depthTest` theo opts (declutter + `scaleLabel` dựa vào đó).
  Nhãn tên chibi mặc định `scale 1.05` (đang 1.2), điều phối viên 1.25. `bubbleTexture`: canvas 2× (256), bong bóng bo + bóng đổ, cùng
  `toneMapped:false` (đặt ở `chibi.js` khi tạo `SpriteMaterial` của `this.bubble`).
- `boards.js` (B, chỉ style sprite/nhãn): `TextSprite` material thêm `toneMapped:false, fog:false`; `Board.mesh` thêm `fog:false`
  (đã `toneMapped:false`). `drawCard`/`drawApprovalTag` có thể làm mềm (bo 24 px, bóng đổ) nhưng giữ nguyên `hits`, kích thước canvas, nội dung.
- `actors.js`: chỉ phần nhìn (vd. nghiêng người nhẹ khi đi). KHÔNG đổi máy trạng thái, nhịp, `c.group.visible = !this.puppet;` (test grep).

## 10. Ngân sách hiệu năng (đo bằng `#debug3d`, n = 30 agent, 1440×900)
| | Hiện tại | Mục tiêu |
|---|---|---|
| Draw call World | ~1 540 | **≤ 160** (≈ 20 batch + 29 occluder + 2n+2 màn hình + boards/đồng hồ/LED/biển) |
| Draw call chibi | 31 × ~23 | **31 × ≤ 10** |
| Tổng `renderer.info.render.calls` | ~2 250 | **≤ 500** (high), shadow pass chỉ khi `shadowDirty` |
| Tam giác | — | **≤ 400 k** |
| Texture | 1 | ≤ 16, mỗi cái ≤ 512² |
| FPS (iGPU, high) | — | ≥ 30 toàn cảnh, ≥ 25 đi bộ; nếu < 24 hai cửa sổ → tự hạ low |

## 11. Chia file (KHÔNG hai người cùng sửa một file)
**A "môi trường"**: `office.js` (CHỈ: tạo renderer, tone mapping, shadow, env, fog, `applyTheme`, `fitCamera` phần fog, tier/`setQuality`,
khối đo FPS thêm `lowWins`, `shadowDirty`, `setLampsVisible`, `#debug3d`, `webglcontextrestored`), `office/world.js`, `office/materials.js` (mới).
A **không** sửa `util.js` (world.js chuyển từ `mat/box/cyl/sphere` của util sang `Batch`/`surface()`; vẫn được import `seeded, hashStr,
textCanvas, FONT, fitText, roundRect, TEAM_COLORS, NO_TEAM_COLOR` không đổi).
**B "nhân vật"**: `office/chibi.js`, `office/actors.js` (chỉ nhìn), `office/boards.js` (chỉ style sprite/nhãn/thẻ), `office/util.js`
(chỉ `labelSprite`, `bubbleTexture`, `texFromCanvas`; giữ nguyên chữ ký mọi export: `mat, basic, geo, box, cyl, sphere, textCanvas, scaleLabel,
declutter, fitText, roundRect, FONT, EMOJI_FONT, hashStr, seeded, lerpAngle, TEAM_COLORS, NO_TEAM_COLOR`).
Không ai sửa: `walk.js`, `path.js`, `talk.js`, `chat.js`, `flows.js`, `ui.js`, template, CSS, Python.
Giao tiếp duy nhất giữa A và B: renderer bật tone mapping/fog → B đặt `toneMapped:false, fog:false` cho mọi sprite/bảng của mình;
chibi tự hưởng `scene.environment` + shadow map, không cần import gì từ A.

## 12. Không được phá (grep trong `office.js`, `walk.js`, `path.js`, `talk.js`, `chat.js`, tests)
- `export ZONE_NAMES`, `export layoutKey(data)` (không đổi khoá), `new World(data)` **không cần renderer** (tests dựng trong Node với canvas giả).
- Thuộc tính: `group, W, D, zs, rw, openZ0, openZ1, grid` (mọi `grid.rect` giữ nguyên — va chạm đi bộ + A*), `clusters` (`id,color,x,z,cx,cz,w,d,k,members`),
  `cafe` (`x0,x1,z0,z1,rows,k,seats,need`), `zones` + spot (`zone,x,z,rot,pose,seatY,owner,screen,stand,ph`), `seats`, `mgrId`, `mgrSpot`
  (`screen`, `stand`), `rooms` (`key,name,x0,z0,x1,z1,p,door`), `occluders` (**mảng Mesh đơn**, raycast `intersectObjects(…, false)` + đổi `.material`),
  `fadeMat`, `floor` (Mesh, mặt trên y = 0, raycast bấm sàn), `chatAnchor` (`x,y,z,rotY,w,h` — khung màn chat ở vách đông giữ chỗ),
  `boards {kanban, runs, hud, needs}` (`Board`), `focusables`, `exitMeshes` (`userData.exit`), `exitBoard`, `teamColor`, `teamFx`, `estop`,
  `textures`, `signs`, `hemi`, `dir`, `daylight`.
- Phương thức: `seatFor, reserve, release, playCafe, setScreen(id,on)` (`screen.color.set`), `setEstop, setTeamEstop, setLabelScale, setSignsVisible,
  flashClock, update(dt,t,reduced), setDaylight(h), roomAt, fade(set), dispose()` (dispose thêm batch geometry, InstancedMesh, texture
  `tex()` thì KHÔNG dispose — dùng chung giữa các lần dựng; vật liệu `surface()` cũng dùng chung).
- Mới (A thêm): `shadowDirty`, `envInt`, `setTheme(dark)`, `setLampsVisible(on)`, `fill`.
- Chibi: `group (userData.actorId), label, labelScale, pose, walking, seatY, setLabel, setBubble, setRing, setSelected, animate, dispose`, `SIT_POSES`.
- Chuỗi test grep trong office.js/actors.js/walk.js (`tests/test_office_walk.py`, `tests/test_office_coordinator.py`) — đừng sửa các dòng đó.
- `prefers-reduced-motion`: glow/neon/screens tĩnh, không dao động bóng; ESTOP như cũ.
- Lệnh kiểm phải xanh: `node tests/office_layout_check.mjs .`, `node tests/office_coordinator_check.mjs .`, `node tests/office_walk_ui_check.mjs .`,
  `pytest tests/test_web_office.py tests/test_office_walk.py tests/test_office_coordinator.py tests/test_office_talk.py`
  (có `node --check` mọi file `office/*.js`).

## 13. Nghiệm thu (QA chụp ảnh qua Playwright, cửa sổ 1440×900, ≥ 20 agent)
Chụp: (1) toàn cảnh light theme ban ngày, (2) toàn cảnh dark theme, (3) đi bộ góc thứ nhất trong cụm bàn, (4) cafe ban đêm
(giả giờ: `Date` override trong `page.addInitScript` → 22:00), (5) tier low (`sessionStorage.cfh_office_q = 'low'`).
- [ ] Có bóng đổ mềm của bàn/ghế/tủ trên sàn (high) và AO dưới chân đồ đạc (cả low); không còn đồ "trôi".
- [ ] Sàn có vân ván gỗ nhìn ra từng tấm; tường có mặt cắt than ở đỉnh; nhà nằm trên bệ, có nền đất tan vào fog, không mép cắt trên nền trắng.
- [ ] Ghế có chân sao 5 cánh, bàn có chân khung + máng cáp, màn hình có viền + nội dung "code" khi agent làm việc.
- [ ] Toàn cảnh không còn chấm đen đèn thả; ban đêm có vũng sáng ấm dưới đèn, neon cafe có quầng sáng; ban ngày cửa sổ sáng + vệt nắng.
- [ ] Không còn biển sprite phòng/team lơ lửng; tên phòng đọc được trên vách, tên team trên thảm/cờ; nhãn agent sắc nét, không chồng.
- [ ] Nhân vật: bóng mờ dưới chân, mặt có lông mày + mắt có điểm sáng, chất liệu bóng nhẹ; màu áo vẫn phân biệt team.
- [ ] Dark theme: nền + fog + nền đất tối, chữ/bảng không bị tone mapping làm xỉn (HUD, kanban, thẻ, nhãn giữ màu như cũ).
- [ ] Đi bộ: tường che camera vẫn mờ (fade), bấm sàn vẫn đi, bấm nhân vật/bảng/cửa vẫn đúng.
- [ ] ESTOP: đèn đỏ toàn văn phòng; team ESTOP: lớp đỏ + nón đỏ; reduced-motion: không nhấp nháy.
- [ ] `#debug3d`: `calls ≤ 500`, `triangles ≤ 400k` ở toàn cảnh n ≥ 20; FPS ≥ 30 trên máy dev (để không giới hạn 30 FPS che khuất:
      đọc `fps` khung đo). Ép chậm (DevTools CPU 6× + GPU) → tự hạ `tier: "low"` sau ~10 s, không rơi vào fallback.
- [ ] Mọi lệnh ở §12 xanh.

## 14. Thứ tự làm đề xuất
A: (1) `materials.js` (Batch, roundedBoxGeo, surface, tex) + chuyển `world.js` sang batch, chưa đổi hình — đo draw call; (2) renderer ACES +
env + shadow tĩnh + DAYLIGHT mới + fog/nền/bệ; (3) hình khối từng khu; (4) biển kiến trúc, đèn LED + vũng sáng, AO, glow; (5) tier + debug.
B: (1) gộp khớp + vật liệu vinyl + blob (đo draw call); (2) tỉ lệ/mặt/tóc; (3) `toneMapped/fog` cho sprite + style nhãn/bong bóng/thẻ.
Mỗi bước chạy lại §12. Chưa kiểm hình thật bằng trình duyệt thì ghi rõ "chưa kiểm hình" trong báo cáo (như `office-walk.md`).

## 15. Ghi chú triển khai A (môi trường) — chỗ lệch spec
- **Texture "chi tiết"**: `woodLight/walnut/plaster/carpet/fabric` vẽ trên nền TRẮNG (chỉ vân/hạt) — màu nằm ở vertex color, nên một vật liệu
  `wood`/`walnut` phủ được nhiều tông gỗ. `oakFloor/terrazzo/tiles` giữ màu trong texture (vertex trắng). UV box-projection theo mét, không dùng `repeat`.
- **Ánh sáng**: đo thực tế (ảnh chụp + so pixel) cho thấy env PMREM + hemi theo bảng §8 làm bóng gần như biến mất sau ACES → bảng DAYLIGHT
  hạ hemi ~0,45, env 0,18, fill 0,15, dir 2,7 (trưa), exposure 1,0 (dark 0,92); ban đêm nâng hemi 1,0 để còn đọc mặt nhân vật.
  Quỹ đạo nắng lệch pha +0,7 rad và thấp hơn (`L·(0,36 + 0,26 sin φ)`) → trưa nắng từ tây-nam, bóng đổ chéo sang đông thấy được từ camera toàn cảnh.
- **Bóng tĩnh**: `shadowDirty` → office.js vẽ lại shadow map 3 khung liền (`shadowFrames`), không chỉ 1.
- **Fog**: tính mỗi khung theo khoảng cách camera→target (nội suy giữa `40/140` khi gần/đi bộ và `1,05·d / 2,4·d` ở toàn cảnh), không chỉ trong `fitCamera`.
- **AO / vũng đèn / quầng** đặt ở y 0,021–0,026 (trên mặt thảm 0,012–0,016), không phải 0,004 — nếu thấp hơn sẽ bị thảm che.
- **Cafe** (yêu cầu chủ sản phẩm 2026-11-03 "để đấy xấu quá"): bỏ khối tím + viền neon 3 màu. Gaming lounge tông ấm: sàn thảm tấm graphite
  viền óc chó, vách lửng mặt ngoài sáng / mặt trong ốp óc chó, LED trắng ấm mảnh trên đỉnh vách + MỘT dải accent dịu sát chân vách
  (`rgbMats[0]`, trôi màu chậm trong dải lục lam–chàm, tĩnh khi reduced/ESTOP) có quầng hắt xuống sàn; vách ngăn vải giữa hai hàng bàn,
  bàn óc chó sẫm, ghế gaming ghế-ngồi màu team dịu; biển "Cafe điện tử" tấm than chữ trắng ấm (không neon). `rgbMats`/`cafeMats`/spot/zone/grid giữ nguyên.
  QA (2026-11-03): ở toàn cảnh khu này vẫn đọc như một khối tối → sàn thảm yến mạch ấm `#d3c6b3` + thảm chạy `#b3a28c`, ốp trong óc chó sáng,
  bàn óc chó vừa, vách ngăn xanh xô thơm, lưng ghế màu team pha than, thùng máy trắng ngà, đèn phím chỉ là vạch mảnh, cây cao hai đầu dãy (khi dư ≥ 0,9 m).
- **Declutter** (`util.js`, được phép sau khi B xong): vùng chiếm của nhãn tính thêm khoảng bong bóng emoji phía trên (1,2× cao nhãn) → cafe đông không còn nhãn đè bong bóng.
- Đo (n = 24 agent + điều phối viên, 1440×900, Iris Xe, high): toàn cảnh 308–366 draw call, ~350–376 k tam giác (World ~165 k), 29,9 FPS (giới hạn 30).
