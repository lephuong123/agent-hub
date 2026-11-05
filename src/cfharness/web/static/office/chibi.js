/* Văn phòng 3D — nhân vật chibi kiểu tượng vinyl (designer toy), dựng thủ tục từ hình khối (đầu to, tay chân ngắn có khớp).
 * Mô hình nhìn về +z khi rotation.y = 0. Tư thế = góc đích cho từng khớp, nội suy mượt + dao động cộng thêm (đi, gõ phím…).
 * Cả thân là MỘT SkinnedMesh gộp mọi chi tiết (màu ở vertex color), mỗi khớp là một Bone cứng (trọng số 1): body, chân trái/phải,
 * tay trái/phải, đầu, mắt (chớp = scale.y của bone mắt) → 1 draw call / chibi + nhãn/bong bóng khi hiện. Bóng blob của MỌI chibi là một
 * InstancedMesh chung (1 draw call cho cả văn phòng), ma trận cập nhật mỗi khung trong scene.onBeforeRender (docs/dev-notes/office-3d-visual.md §9, §15). */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { hashStr, seeded, mat, basic, geo, labelSprite, bubbleTexture, scaleLabel } from './util.js';

const SKIN = ['#f7d7c4', '#f1c6a6', '#e0ac87', '#c98f68', '#a8714f', '#fbe1cf'];
const HAIR = ['#3b2a20', '#5a3b28', '#1f1b1a', '#8a5a3b', '#d8a657', '#b4513a', '#6b4f8a', '#2f4858', '#e7c9a0'];
const PANTS = ['#4a4e69', '#3d405b', '#5c4d3c', '#2f3e46', '#6d597a'];
const STYLES = ['short', 'bob', 'pony', 'buns', 'spiky', 'ahoge'];
const SHOE = '#2d2622', SOLE = '#d9d2c5', VEST = '#3a3f58', GEAR = '#2d2d33';

export const SIT_POSES = new Set(['sit', 'type', 'typeFast', 'headInHands', 'sleep', 'relax', 'sitDrink', 'sitThink', 'sitRead', 'sitPhone']);
const HIP_Y = 0.26;
// sọ: elipxoit bán trục (A, B, C), tâm y = SKULL_Y trong nhóm đầu
const SA = 0.3, SB = 0.279, SC = 0.285, SKULL_Y = 0.29;
const BLOB_R = 0.34, BLOB_SIT = 0.42 / 0.34;

// Vật liệu dùng chung (một lần cho mọi chibi). Màu từng chi tiết nằm ở vertex color, độ nhám ở attribute `rough`
// (mắt 0.15 bóng, còn lại 0.6) → cả chibi 1 vật liệu = 1 draw call. Tier 'low' (setChibiQuality): Lambert + vertex color, không rim.
let _mats = null, _quality = 'high';
function mats() {
  if (_mats) return _mats;
  const vinyl = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.6, metalness: 0 });
  vinyl.onBeforeCompile = (sh) => {
    sh.vertexShader = 'attribute float rough;\nvarying float vRough;\n' + sh.vertexShader.replace('#include <begin_vertex>', '#include <begin_vertex>\nvRough = rough;');
    sh.fragmentShader = 'varying float vRough;\n' + sh.fragmentShader
      .replace('#include <roughnessmap_fragment>', '#include <roughnessmap_fragment>\nroughnessFactor = vRough;')
      // viền sáng nhẹ ở mép (kiểu nhựa vinyl dưới đèn softbox) — rẻ, không thêm draw call; mắt (nhám thấp) không có viền
      .replace('#include <opaque_fragment>',
        'outgoingLight += vec3(1.0, 0.96, 0.9) * pow(1.0 - saturate(dot(normal, normalize(vViewPosition))), 3.0) * 0.2 * step(0.3, vRough);\n#include <opaque_fragment>');
  };
  vinyl.customProgramCacheKey = () => 'cfh-vinyl-rim2';
  const low = new THREE.MeshLambertMaterial({ vertexColors: true });
  _mats = { vinyl, low };
  return _mats;
}
const bodyMat = () => (_quality === 'low' ? mats().low : mats().vinyl);

const _live = new Set(); // chibi đang sống — đổi tier áp cho cả chibi có sẵn, bóng blob lặp qua đây

// LOD nhân vật theo cỡ trên màn hình: chiều cao chibi (≈ 1,4 m) chiếu ra < LOD_FAR_PX px → hình xa (≈ 1/4 tam giác); > LOD_NEAR_PX → hình gần
// (trễ giữa hai mốc để không nhấp nháy). Toàn cảnh 20–40 agent: chibi cao ~40–70 px → hình xa; bay tới / đi bộ gần: hình gần.
// viewH = chiều cao khung vẽ (CSS px). Gọi mỗi khung trước khi vẽ.
const LOD_FAR_PX = 80, LOD_NEAR_PX = 96;
// chế độ đi bộ (camera thấp, nhiều nhân vật ở tầm trung): mốc cao hơn → nhân vật cao < 160 px dùng hình xa (ít đỉnh skinning,
// ít draw-time CPU/GPU); nhìn gần (> 190 px) vẫn hình gần
const LOD_WALK = [160, 190];
let _lod = [LOD_FAR_PX, LOD_NEAR_PX];
export function setChibiLodWalk(on) { _lod = on ? LOD_WALK : [LOD_FAR_PX, LOD_NEAR_PX]; }
export function updateChibiLod(camera, viewH) {
  const ppu = (1.4 * viewH) / (2 * Math.tan((camera.fov * Math.PI) / 360));
  const cam = camera.matrixWorld.elements;
  for (const c of _live) {
    if (!c.geoFar) continue;
    const e = c.group.matrixWorld.elements;
    const px = ppu / Math.max(0.1, Math.hypot(e[12] - cam[12], e[13] - cam[13], e[14] - cam[14]));
    const far = c.far ? px < _lod[1] : px < _lod[0];
    if (far !== c.far) { c.far = far; c.mesh.geometry = far ? c.geoFar : c.geoNear; }
  }
}

// Tier high: World không dùng IBL (office-3d-visual.md §3, phase 2) — chỉ vật liệu vinyl của nhân vật giữ env map riêng (highlight).
export function setChibiEnv(tex, intensity) {
  const v = mats().vinyl;
  if (v.envMap !== tex) { v.envMap = tex || null; v.needsUpdate = true; }
  if (intensity != null) v.envMapIntensity = intensity;
}
// Tier chất lượng nhân vật: 'high' = vinyl PBR + rim, 'low' = Lambert vertex color. Gọi được trước khi có chibi nào và gọi lặp lại.
export function setChibiQuality(tier) {
  const q = tier === 'low' ? 'low' : 'high';
  if (q === _quality) return;
  _quality = q;
  const m = bodyMat();
  for (const c of _live) for (const mesh of c._mats) mesh.material = m;
}

// Bóng blob dưới chân: radial đen 0.45 → 0. MỌI chibi chung một InstancedMesh (1 draw call); `chibi.shadow` vẫn là Mesh riêng
// nhưng visible = false (chỉ để bấm/raycast như cũ — Raycaster không xét visible).
let _blob = null;
function blobMat() {
  if (_blob) return _blob;
  const c = document.createElement('canvas');
  c.width = c.height = 64;
  const ctx = c.getContext('2d');
  const gr = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
  gr.addColorStop(0, 'rgba(0,0,0,0.45)'); gr.addColorStop(0.55, 'rgba(0,0,0,0.3)'); gr.addColorStop(1, 'rgba(0,0,0,0)');
  ctx.fillStyle = gr; ctx.fillRect(0, 0, 64, 64);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  _blob = new THREE.MeshBasicMaterial({ map: t, transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2,
    toneMapped: false, fog: false });
  return _blob;
}
const blobGeo = () => geo('blob', () => new THREE.PlaneGeometry(BLOB_R * 2, BLOB_R * 2));
let _blobs = null;
const _bm = new THREE.Matrix4(), _bw = new THREE.Matrix4(), _sv = new THREE.Vector3();
// Gắn InstancedMesh bóng vào scene chứa chibi (lần đầu / khi scene đổi / khi cần thêm chỗ) + móc cập nhật trước mỗi lần vẽ.
function ensureBlobs(scene) {
  const need = Math.max(32, _live.size);
  if (_blobs && _blobs.parent === scene && _blobs.instanceMatrix.count >= need) return;
  let cap = _blobs ? _blobs.instanceMatrix.count : 32;
  while (cap < need) cap *= 2;
  if (_blobs) { _blobs.removeFromParent(); _blobs.dispose(); }
  _blobs = new THREE.InstancedMesh(blobGeo(), blobMat(), cap);
  _blobs.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  _blobs.count = 0; _blobs.frustumCulled = false; _blobs.renderOrder = 1; _blobs.name = 'chibiBlobs';
  _blobs.raycast = () => {}; // không bấm được (bấm qua chibi.shadow như cũ)
  scene.add(_blobs);
  if (!scene.userData.cfhBlobHook) {
    const prev = scene.onBeforeRender;
    scene.onBeforeRender = function (...a) { if (prev) prev.apply(this, a); syncBlobs(this); };
    scene.userData.cfhBlobHook = true;
  }
}
function syncBlobs(scene) {
  if (!_blobs || _blobs.parent !== scene) return;
  let n = 0;
  for (const c of _live) {
    let o = c.group, ok = false;
    while (o && o.visible) { if (o === scene) { ok = true; break; } o = o.parent; }
    if (!ok) continue;
    const s = c._blobS;
    _bm.makeRotationX(-Math.PI / 2).scale(_sv.set(s, s, s)).setPosition(0, 0.006, 0);
    _blobs.setMatrixAt(n++, _bw.multiplyMatrices(c.group.matrixWorld, _bm));
    if (n >= _blobs.instanceMatrix.count) break;
  }
  _blobs.count = n;
  _blobs.instanceMatrix.needsUpdate = true;
}

function shade(hex, k) {
  const c = new THREE.Color(hex);
  const hsl = {}; c.getHSL(hsl);
  c.setHSL(hsl.h, Math.min(1, hsl.s * (0.85 + 0.3 * k)), Math.max(0.18, Math.min(0.8, hsl.l + (k - 0.5) * 0.18)));
  return '#' + c.getHexString();
}
function mix(a, b, k) { return '#' + new THREE.Color(a).lerp(new THREE.Color(b), k).getHexString(); }
function darker(hex, k) { const c = new THREE.Color(hex); c.multiplyScalar(k); return '#' + c.getHexString(); }

// Mức chi tiết (LOD) khi dựng hình: _k = 1 (gần) hoặc LOD_K (xa — số phân đoạn ×0,5, ≈ 1/4 tam giác). S(n) = số phân đoạn theo mức;
// G(key, …) = geo() có khoá theo mức. docs/dev-notes/office-3d-perf.md
const LOD_K = 0.5;
let _k = 1;
const S = (n, min) => Math.max(min || 3, Math.round(n * _k));
const G = (key, make) => geo(key + '@' + _k, make);
function capsule(r, len) { return G('cap' + r + ':' + len, () => new THREE.CapsuleGeometry(r, len, S(3, 2), S(10))); }
function hairCap(r, theta) { return G('hair' + r + ':' + theta, () => new THREE.SphereGeometry(r, S(20), S(11), 0, Math.PI * 2, 0, theta)); }
const sph = (r, ws, hs) => G('vs' + r + ':' + ws + ':' + hs, () => new THREE.SphereGeometry(r, S(ws), S(hs)));

// thân: tiện xoay profile bo (hông 0.16 → eo 0.14 → vai 0.17 → cổ), cao 0.3, gốc y = 0
function torsoGeo() {
  return G('torsoLathe', () => {
    const P = [[0, 0], [0.1, 0.002], [0.148, 0.018], [0.16, 0.05], [0.152, 0.1], [0.142, 0.145], [0.152, 0.2], [0.168, 0.245],
      [0.166, 0.275], [0.14, 0.3], [0.09, 0.318], [0.05, 0.324], [0, 0.326]];
    return new THREE.LatheGeometry(P.map(([x, y]) => new THREE.Vector2(x, y)), S(16));
  });
}

const _q = new THREE.Quaternion(), _e = new THREE.Euler(), _v = new THREE.Vector3(), _s = new THREE.Vector3(), _z = new THREE.Vector3(0, 0, 1);
// ma trận từ vị trí + góc Euler + tỉ lệ
function M(x, y, z, rx, ry, rz, sx, sy, sz) {
  _e.set(rx || 0, ry || 0, rz || 0);
  return new THREE.Matrix4().compose(_v.set(x, y, z), _q.setFromEuler(_e), _s.set(sx || 1, sy == null ? sx || 1 : sy, sz == null ? sx || 1 : sz));
}
// điểm trên mặt sọ theo hướng (dx, dy, dz), đẩy ra `off` theo pháp tuyến → ma trận đặt chi tiết "dán" lên mặt (trục z = pháp tuyến)
function onSkull(dx, dy, dz, off, spin, sx, sy, sz) {
  const d = new THREE.Vector3(dx, dy, dz).normalize();
  const t = 1 / Math.sqrt((d.x / SA) ** 2 + (d.y / SB) ** 2 + (d.z / SC) ** 2);
  const p = d.multiplyScalar(t);
  const n = new THREE.Vector3(p.x / (SA * SA), p.y / (SB * SB), p.z / (SC * SC)).normalize();
  p.addScaledVector(n, off || 0); p.y += SKULL_Y;
  const q = new THREE.Quaternion().setFromUnitVectors(_z, n);
  if (spin) q.multiply(new THREE.Quaternion().setFromAxisAngle(_z, spin));
  return new THREE.Matrix4().compose(p, q, _s.set(sx || 1, sy || 1, sz || 1));
}

// Gộp các chi tiết [{ g: geometry, m: Matrix4, c: màu, r?: độ nhám (mặc định 0.6), b?: chỉ số bone }] thành 1 BufferGeometry
// non-indexed: position, normal, color, rough (+ skinIndex/skinWeight cứng khi skin = true).
function mergeColored(parts, skin) {
  const list = [];
  let n = 0;
  for (const p of parts) {
    const g = p.g.index ? p.g.toNonIndexed() : p.g.clone();
    g.applyMatrix4(p.m);
    list.push([g, new THREE.Color(p.c), p.r == null ? 0.6 : p.r, p.b || 0]);
    n += g.attributes.position.count;
  }
  const pos = new Float32Array(n * 3), nor = new Float32Array(n * 3), col = new Float32Array(n * 3), rough = new Float32Array(n);
  const si = skin ? new Uint16Array(n * 4) : null, sw = skin ? new Float32Array(n * 4) : null;
  let o = 0;
  for (const [g, c, r, b] of list) {
    const cnt = g.attributes.position.count;
    pos.set(g.attributes.position.array, o * 3);
    nor.set(g.attributes.normal.array, o * 3);
    for (let i = 0; i < cnt; i++) {
      const j = o + i;
      col[j * 3] = c.r; col[j * 3 + 1] = c.g; col[j * 3 + 2] = c.b; rough[j] = r;
      if (skin) { si[j * 4] = b; sw[j * 4] = 1; }
    }
    o += cnt;
    g.dispose();
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  out.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  out.setAttribute('color', new THREE.BufferAttribute(col, 3));
  out.setAttribute('rough', new THREE.BufferAttribute(rough, 1));
  if (skin) { out.setAttribute('skinIndex', new THREE.Uint16BufferAttribute(si, 4)); out.setAttribute('skinWeight', new THREE.BufferAttribute(sw, 4)); }
  out.computeBoundingSphere();
  return out;
}

export class Chibi {
  constructor(opts) {
    const h = hashStr(opts.id), rnd = seeded(h);
    this.isManager = !!opts.isManager;
    const skin = SKIN[h % SKIN.length];
    const hairC = HAIR[(h >>> 3) % HAIR.length];
    const shirt = shade(mix(opts.color, '#8a8580', 0.2), rnd());
    const pants = PANTS[(h >>> 7) % PANTS.length];
    const style = STYLES[(h >>> 11) % STYLES.length];
    this._geos = [];
    this._mats = []; // mesh dùng vật liệu thân (đổi khi đổi tier)

    const g = this.group = new THREE.Group();
    g.userData.actorId = opts.id;
    // khung xương cứng: mỗi khớp một Bone (vẫn là Object3D — đạo cụ gắn vào tay như cũ)
    const bone = (parent, x, y, z) => { const o = new THREE.Bone(); o.position.set(x, y, z); parent.add(o); return o; };
    const body = this.body = bone(g, 0, 0, 0);
    this.legL = bone(body, -0.09, HIP_Y, 0); this.legR = bone(body, 0.09, HIP_Y, 0);
    this.armL = bone(body, -0.2, 0.53, 0); this.armR = bone(body, 0.2, 0.53, 0);
    const head = this.head = bone(body, 0, 0.6, 0);
    const eyeY = onSkull(0.105, -0.04, 0.265, 0).elements[13];
    const eyes = bone(head, 0, eyeY, 0);
    const bones = [body, this.legL, this.legR, this.armL, this.armR, head, eyes];
    // bóng blob dưới chân (không dùng shadow map): vẽ chung qua InstancedMesh; mesh riêng ẩn chỉ để raycast như cũ
    const sh = new THREE.Mesh(blobGeo(), blobMat());
    sh.rotation.x = -Math.PI / 2; sh.position.y = 0.006; sh.renderOrder = 1; sh.visible = false; g.add(sh);
    this.shadow = sh;
    this._blobS = 1;

    // cốc cà phê và quyển sách gắn vào tay phải (ẩn mặc định)
    this.cup = new THREE.Group();
    const cupMesh = new THREE.Mesh(geo('cupM', () => mergeColored([
      { g: new THREE.CylinderGeometry(0.045, 0.038, 0.09, 12), m: M(0, 0, 0), c: '#ffffff' },
      { g: new THREE.CircleGeometry(0.04, 12), m: M(0, 0.046, 0, -Math.PI / 2, 0, 0), c: '#6b4226' },
      { g: new THREE.TorusGeometry(0.025, 0.008, 6, 10), m: M(0.048, 0, 0), c: '#ffffff' },
    ])), bodyMat());
    this._mats.push(cupMesh);
    this.cup.add(cupMesh);
    this.cup.position.set(0, -0.24, 0.05); this.cup.visible = false; this.armR.add(this.cup);
    this.book = new THREE.Mesh(UNIT(), mat(['#c8413a', '#4f8fc0', '#7fb069', '#e9b44c'][h % 4]));
    this.book.scale.set(0.2, 0.03, 0.15); this.book.position.set(-0.08, -0.25, 0.06); this.book.visible = false; this.armR.add(this.book);
    // điện thoại (rảnh ngồi bàn xem điện thoại — tư thế sitPhone)
    this.phone = new THREE.Mesh(UNIT(), mat('#2b2d33'));
    this.phone.scale.set(0.07, 0.012, 0.13); this.phone.position.set(-0.04, -0.25, 0.05); this.phone.visible = false; this.armR.add(this.phone);

    // Mọi chi tiết thân (gộp thành 1 SkinnedMesh) — dựng 2 mức chi tiết: gần (_k = 1) và xa (LOD_K); đổi geometry theo cỡ trên màn hình
    // (updateChibiLod). Cùng xương, cùng vật liệu → không thêm draw call.
    const top = this.isManager ? VEST : shirt;
    const brow = mix(darker(hairC, 0.6), '#2a1d17', 0.45);
    const locksOf = style === 'spiky' ? [[0, 0.3, 0.95, 0]] : [[-0.17, 0.31, 0.9, 0.35], [0.0, 0.35, 0.95, 0], [0.18, 0.31, 0.9, -0.35]];
    const buildParts = () => {
      const parts = [];
      const add = (b, list) => { for (const q of list) { q.b = b; parts.push(q); } };

      // chân: ống quần + giày bo + đế sáng
      for (const b of [1, 2]) add(b, [
        { g: capsule(0.075, 0.1), m: M(0, -0.11, 0), c: pants },
        { g: sph(0.08, 12, 8), m: M(0, -0.205, 0.035, 0, 0, 0, 1, 0.55, 1.4), c: SHOE },
        { g: G('sole', () => new THREE.CylinderGeometry(0.079, 0.079, 0.018, S(12))), m: M(0, -0.247, 0.035, 0, 0, 0, 1, 1, 1.4), c: SOLE },
      ]);

      // thân: áo (lathe) + cạp quần + cổ áo; điều phối viên: vest + sơ mi chữ V + cà vạt
      const tparts = [
        { g: torsoGeo(), m: M(0, 0.27, 0, 0, 0, 0, 1, 1, 0.85), c: top },
        { g: G('waist', () => new THREE.CylinderGeometry(0.158, 0.163, 0.07, S(16))), m: M(0, 0.29, 0, 0, 0, 0, 1, 1, 0.85), c: pants },
      ];
      if (this.isManager) {
        tparts.push({ g: G('vee', () => new THREE.ConeGeometry(0.07, 0.16, 3)), m: M(0, 0.52, 0.128, Math.PI, 0, 0, 1, 1, 0.5), c: '#ffffff' });
        tparts.push({ g: G('tie', () => new THREE.ConeGeometry(0.03, 0.18, S(4))), m: M(0, 0.47, 0.142, Math.PI, 0, 0, 1, 1, 0.5), c: '#c8413a' });
        tparts.push({ g: G('knot', () => new THREE.SphereGeometry(0.022, S(8), S(6))), m: M(0, 0.565, 0.13), c: '#c8413a' });
      } else {
        tparts.push({ g: G('collar', () => new THREE.TorusGeometry(0.105, 0.02, S(6), S(20))), m: M(0, 0.585, 0, Math.PI / 2, 0, 0, 1, 0.85, 1), c: '#f6f3ee' });
      }
      add(0, tparts);

      // tay: tay áo + bàn tay hơi dẹt
      for (const b of [3, 4]) add(b, [
        { g: capsule(0.056, 0.115), m: M(0, -0.1, 0), c: top },
        { g: sph(0.06, 10, 8), m: M(0, -0.222, 0, 0, 0, 0, 0.82, 1, 0.95), c: skin },
      ]);
      // đầu: sọ + tai + má hồng + miệng + lông mày + tóc (+ tai nghe điều phối viên)
      const hp = [
        { g: sph(0.3, 24, 16), m: M(0, SKULL_Y, 0, 0, 0, 0, SA / 0.3, SB / 0.3, SC / 0.3), c: skin },
        { g: G('mouth', () => new THREE.TorusGeometry(0.028, 0.008, S(6), S(12), Math.PI)), m: onSkull(0, -0.15, 1, 0.002, Math.PI), c: '#7a3b2e' },
      ];
      for (const s of [-1, 1]) {
        hp.push({ g: sph(0.055, 8, 6), m: M(s * 0.292, SKULL_Y - 0.03, -0.01, 0, 0, 0, 0.45, 0.9, 0.75), c: skin }); // tai
        hp.push({ g: G('blush', () => new THREE.CircleGeometry(0.048, S(16))), m: onSkull(s * 0.175, -0.1, 0.25, 0.003, 0, 1, 0.68, 1), c: mix(skin, '#f39a9a', 0.4) });
        hp.push({ g: G('brow', () => new THREE.BoxGeometry(0.068, 0.017, 0.03)), m: onSkull(s * 0.11, 0.042, 0.26, 0.006, s * -0.14), c: brow });
      }
      // tóc: chỏm cầu nghiêng ra sau (che đỉnh + gáy), mái trước, lọn mái dẹt (bớt "mũ bảo hiểm")
      hp.push({ g: hairCap(0.318, style === 'bob' ? 2.05 : 1.8), m: M(0, SKULL_Y + 0.01, 0, -0.6, 0, 0, 1, 0.95, 0.97), c: hairC });
      hp.push({ g: hairCap(0.322, 0.75), m: M(0, SKULL_Y + 0.01, 0, 0.35, 0, 0, 1, 0.95, 0.97), c: hairC });
      for (const [x, y, z, rz] of locksOf) {
        hp.push({ g: sph(0.085, 10, 6), m: onSkull(x, y, z, 0.012, rz, 1.05, 0.72, 0.32), c: hairC });
      }
      if (style === 'bob') for (const s of [-1, 1]) hp.push({ g: sph(0.1, 12, 10), m: M(s * 0.27, SKULL_Y - 0.08, -0.04, 0, 0, s * 0.15, 0.5, 1.15, 0.95), c: hairC });
      if (style === 'pony') {
        hp.push({ g: capsule(0.08, 0.2), m: M(0, 0.32, -0.36, 0.55, 0, 0), c: hairC });
        hp.push({ g: G('tie2', () => new THREE.TorusGeometry(0.06, 0.018, S(6), S(14))), m: M(0, 0.4, -0.3, 0.55 + Math.PI / 2, 0, 0), c: '#e07a5f' });
      } else if (style === 'buns') {
        for (const sx of [-0.21, 0.21]) hp.push({ g: sph(0.1, 14, 10), m: M(sx, 0.52, -0.05), c: hairC });
      } else if (style === 'spiky') {
        const cg = G('spike', () => new THREE.ConeGeometry(0.07, 0.17, S(8)));
        for (let i = 0; i < 5; i++) {
          const a = (i - 2) * 0.35;
          hp.push({ g: cg, m: M(Math.sin(a) * 0.2, 0.55 + Math.cos(a) * 0.04, -0.04 - Math.abs(a) * 0.05, -0.25, 0, -a * 0.9), c: hairC });
        }
      } else if (style === 'ahoge') {
        hp.push({ g: G('ahoge', () => new THREE.TorusGeometry(0.07, 0.016, S(6), S(12), Math.PI * 1.2)), m: M(0.02, 0.66, 0, 0, Math.PI / 2, 0), c: hairC });
      }
      if (this.isManager) {
        // tai nghe: vòng qua đầu + chụp tai + mic
        hp.push({ g: G('band', () => new THREE.TorusGeometry(0.33, 0.022, S(8), S(24), Math.PI)), m: M(0, 0.3, 0), c: GEAR });
        for (const sx of [-0.32, 0.32]) hp.push({ g: G('ear', () => new THREE.CylinderGeometry(0.075, 0.075, 0.05, S(16))), m: M(sx, 0.28, 0, 0, 0, Math.PI / 2), c: GEAR });
        hp.push({ g: capsule(0.012, 0.2), m: M(0.25, 0.2, 0.14, 1.3, 0, 0.6), c: GEAR });
        hp.push({ g: G('mictip', () => new THREE.SphereGeometry(0.025, S(8), S(6))), m: M(0.17, 0.15, 0.24), c: '#c8413a' });
      }
      add(5, hp);

      // mắt (bone riêng — chớp = scale.y): lòng đen nâu sẫm + con ngươi + 2 điểm sáng, nhám thấp (bóng), không viền rim
      const ep = [];
      for (const s of [-1, 1]) {
        const base = onSkull(s * 0.105, -0.04, 0.265, -0.004);
        base.elements[13] -= eyeY; // về gốc bone mắt
        const at = (x, y, z, sx, sy, sz) => base.clone().multiply(M(x, y, z, 0, 0, 0, sx, sy, sz));
        ep.push({ g: sph(0.05, 12, 9), m: at(0, 0, 0, 0.85, 1.15, 0.45), c: '#3a2519', r: 0.15 });
        ep.push({ g: sph(0.03, 8, 6), m: at(0, -0.008, 0.012, 0.85, 1.1, 0.45), c: '#120c09', r: 0.15 });
        ep.push({ g: sph(0.016, 6, 4), m: at(0.014, 0.024, 0.02, 1, 1, 0.5), c: '#ffffff', r: 0.15 });
        ep.push({ g: sph(0.008, 5, 4), m: at(-0.012, -0.022, 0.02, 1, 1, 0.5), c: '#ffffff', r: 0.15 });
      }
      add(6, ep);
    return parts;
    };
    this.eyes = [eyes];

    // gộp mọi chi tiết về không gian nhóm (tư thế gốc) → 1 SkinnedMesh, mỗi đỉnh bám cứng 1 bone
    g.updateMatrixWorld(true);
    const merged = (k) => {
      _k = k;
      try {
        const parts = buildParts();
        for (const q of parts) q.m = bones[q.b].matrixWorld.clone().multiply(q.m);
        const bg = mergeColored(parts, true);
        this._geos.push(bg);
        return bg;
      } finally { _k = 1; }
    };
    this.geoNear = merged(1); this.geoFar = merged(LOD_K); this.far = false;
    const sk = this.mesh = new THREE.SkinnedMesh(this.geoNear, bodyMat());
    sk.receiveShadow = true;
    // bao đủ mọi tư thế (tay giơ cao, ngồi): frustum cull + lọc raycast không phải tính lại theo xương
    sk.boundingSphere = new THREE.Sphere(new THREE.Vector3(0, 0.62, 0), 0.95);
    g.add(sk);
    g.updateMatrixWorld(true);
    sk.bind(new THREE.Skeleton(bones), sk.matrixWorld);
    this._mats.push(sk);
    _live.add(this);

    // vòng dưới chân: đỏ = chờ bạn duyệt; cam = đang chọn
    const ringO = { transparent: true, depthWrite: false, side: THREE.DoubleSide, forceSinglePass: true, toneMapped: false, fog: false }; // 1 lượt: không tính lại program mỗi khung
    this.ringRed = new THREE.Mesh(geo('ringR', () => new THREE.RingGeometry(0.36, 0.46, 32)), basic('#e0443a', Object.assign({ opacity: 0.85 }, ringO)));
    this.ringRed.rotation.x = -Math.PI / 2; this.ringRed.position.y = 0.02; this.ringRed.visible = false; g.add(this.ringRed);
    this.ringSel = new THREE.Mesh(geo('ringS', () => new THREE.RingGeometry(0.5, 0.58, 40)), basic('#d97757', Object.assign({ opacity: 0.95 }, ringO)));
    this.ringSel.rotation.x = -Math.PI / 2; this.ringSel.position.y = 0.025; this.ringSel.visible = false; g.add(this.ringSel);

    // nhãn tên + bong bóng
    this.label = null;
    this.setLabel(opts.name, opts.color);
    this.bubble = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, depthWrite: false, toneMapped: false, fog: false }));
    this.bubble.scale.set(0.55, 0.55, 1); this.bubble.position.y = 2.0; this.bubble.visible = false; this.bubble.renderOrder = 6;
    g.add(this.bubble);
    this._bubbleKey = '';

    if (this.isManager) g.scale.setScalar(1.08);
    this.pose = 'stand';
    this.cur = { lL: 0, lR: 0, aLx: 0, aRx: 0, aLz: 0.12, aRz: -0.12, hx: 0, hz: 0, hy: 0, by: 0, lean: 0 };
    this.phase = rnd() * 10; this.walkPhase = 0; this.blinkAt = 1 + rnd() * 4; this.blink = 0;
    this.walking = false; this.seatY = 0.3;
  }

  setLabel(name, color) {
    if (this._labelText === name + '|' + color) return;
    this._labelText = name + '|' + color;
    if (this.label) { this.group.remove(this.label); this.label.material.map.dispose(); this.label.material.dispose(); }
    this.label = labelSprite(name, color, { scale: this.isManager ? 1.25 : 1.05 });
    this.label.position.y = 1.62;
    this.group.add(this.label);
  }

  setBubble(emoji, tone) {
    const k = emoji ? emoji + '|' + (tone || '') : '';
    if (k === this._bubbleKey) return;
    this._bubbleKey = k;
    if (!emoji) { this.bubble.visible = false; return; }
    this.bubble.material.map = bubbleTexture(emoji, tone);
    this.bubble.material.needsUpdate = true;
    this.bubble.visible = true;
    this._pop = 0; // nảy nhẹ khi đổi bong bóng
  }

  setRing(red) { this.ringRed.visible = !!red; }
  setSelected(on) { this.ringSel.visible = !!on; }

  // Cập nhật khớp theo tư thế. t = thời gian (giây), reduced = giảm chuyển động.
  animate(dt, t, reduced) {
    const p = this.pose, c = this.cur, w = this.walking;
    const sit = SIT_POSES.has(p) && !w;
    // góc đích
    let lL = 0, lR = 0, aLx = 0, aRx = 0, aLz = 0.14, aRz = -0.14, hx = 0, hz = 0, hy = 0, by = 0, lean = 0;
    let eyesClosed = false;
    this.cup.visible = (p === 'drink' || p === 'sitDrink') && !w;
    this.book.visible = (p === 'read' || p === 'sitRead') && !w;
    this.phone.visible = p === 'sitPhone' && !w;
    if (sit) { lL = lR = -1.45; by = this.seatY - HIP_Y + 0.02; }
    switch (w ? 'walk' : p) {
      case 'type': case 'typeFast': case 'standType': aLx = aRx = -1.15; aLz = 0.25; aRz = -0.25; hx = 0.12; break;
      case 'raise': aRx = -2.95; aRz = 0.4; hx = -0.12; hz = -0.1; break;
      case 'headInHands': aLx = aRx = -2.55; aLz = -0.35; aRz = 0.35; hx = 0.38; break;
      case 'sleep': aLx = aRx = -0.35; hx = 0.45; hz = 0.35; eyesClosed = true; break;
      case 'relax': aLz = 0.6; aRz = -0.6; hx = -0.15; break;
      case 'drink': case 'sitDrink': aRx = -1.9; aRz = 0.55; aLx = -0.2; break;
      case 'read': case 'sitRead': aLx = aRx = -1.0; aLz = -0.25; aRz = 0.25; hx = 0.3; break;
      case 'sitPhone': aRx = -1.25; aRz = 0.2; aLx = -0.9; aLz = -0.1; hx = 0.42; break;
      case 'think': case 'sitThink': aRx = -2.3; aRz = 0.6; hz = 0.18; hy = 0.2; break;
      case 'freeze': aLz = 0.05; aRz = -0.05; break;
      default: break;
    }
    // dao động cộng thêm
    this.phase += dt;
    let blob = sit ? BLOB_SIT : 1;
    if (!reduced) {
      if (w) {
        this.walkPhase += dt * 9;
        const s = Math.sin(this.walkPhase);
        lL = s * 0.65; lR = -s * 0.65; aLx = -s * 0.55; aRx = s * 0.55;
        by += Math.abs(Math.cos(this.walkPhase)) * 0.05;
        lean = 0.07; // nghiêng người nhẹ về trước khi đi
        blob *= 1 - Math.abs(Math.cos(this.walkPhase)) * 0.1 + 0.05; // bóng co giãn ±5 % theo nhịp nảy
      } else if (p === 'type' || p === 'typeFast' || p === 'standType') {
        const f = p === 'typeFast' ? 24 : 13;
        aLx += Math.sin(this.phase * f) * 0.12; aRx += Math.sin(this.phase * f + 1.7) * 0.12;
        hy = Math.sin(this.phase * 0.7) * 0.08;
      } else if (p === 'raise') {
        aRz += Math.sin(this.phase * 7) * 0.3; by += Math.abs(Math.sin(this.phase * 3.5)) * 0.04;
      } else if (p === 'drink' || p === 'sitDrink') {
        const k = (Math.sin(this.phase * 1.2) + 1) / 2;
        aRx = -1.3 - k * 1.0; hx = -k * 0.15;
      } else if (p === 'sleep') {
        hx += Math.sin(this.phase * 1.3) * 0.05; by += Math.sin(this.phase * 1.3) * 0.008;
      } else if (p === 'headInHands') {
        hz = Math.sin(this.phase * 2.2) * 0.12;
      } else if (p === 'read' || p === 'sitRead') {
        hy = Math.sin(this.phase * 0.9) * 0.15;
      } else if (p !== 'freeze') {
        by += Math.sin(this.phase * 2) * 0.006; // thở
        hy = Math.sin(this.phase * 0.5) * 0.12;
      }
    }
    const k = reduced ? 1 : 1 - Math.exp(-dt * 12);
    c.lL += (lL - c.lL) * k; c.lR += (lR - c.lR) * k;
    c.aLx += (aLx - c.aLx) * k; c.aRx += (aRx - c.aRx) * k; c.aLz += (aLz - c.aLz) * k; c.aRz += (aRz - c.aRz) * k;
    c.hx += (hx - c.hx) * k; c.hz += (hz - c.hz) * k; c.hy += (hy - c.hy) * k; c.by += (by - c.by) * k; c.lean += (lean - c.lean) * k;
    this.legL.rotation.x = c.lL; this.legR.rotation.x = c.lR;
    this.armL.rotation.set(c.aLx, 0, c.aLz); this.armR.rotation.set(c.aRx, 0, c.aRz);
    // tay giơ cao dài ra một chút để thấy rõ trên đầu to
    const ry = p === 'raise' && !w ? 1.55 : 1;
    this.armR.scale.y += (ry - this.armR.scale.y) * k;
    this.head.rotation.set(c.hx, c.hy, c.hz);
    this.body.position.y = c.by;
    this.body.rotation.x = c.lean;
    this.shadow.scale.setScalar(blob);
    this._blobS = blob;
    // bóng blob chung: gắn InstancedMesh vào scene đang chứa chibi (rẻ — chỉ so sánh khi đã gắn)
    let root = this.group;
    while (root.parent) root = root.parent;
    if (root.isScene && (!_blobs || _blobs.parent !== root || _blobs.instanceMatrix.count < _live.size)) ensureBlobs(root);

    // chớp mắt
    let ey = 1;
    if (eyesClosed) ey = 0.12;
    else if (!reduced) {
      this.blinkAt -= dt;
      if (this.blinkAt < 0) { this.blink = 0.14; this.blinkAt = 2.5 + Math.random() * 4; }
      if (this.blink > 0) { this.blink -= dt; ey = 0.15; }
    }
    for (const e of this.eyes) e.scale.y = ey;

    // bong bóng nảy + lắc nhẹ; vòng đỏ nhấp nháy
    if (this.bubble.visible) {
      this._pop = Math.min(1, (this._pop || 0) + dt * 4);
      const s = (reduced ? 0.55 : 0.55 * (0.6 + 0.4 * easeOutBack(this._pop))) * Math.min(1.8, this.labelScale || 1);
      this.bubble.scale.set(s, s, 1);
      this.bubble.position.y = 2.0 + ((this.labelScale || 1) - 1) * 0.35 + (reduced ? 0 : Math.sin(this.phase * 2.4) * 0.04) + (sit ? -0.12 : 0);
    }
    scaleLabel(this.label, this.labelScale || 1);
    this.label.position.y = (sit ? 1.48 : 1.62) + ((this.labelScale || 1) - 1) * 0.12;
    if (this.ringRed.visible) {
      const s = reduced ? 1 : 1 + Math.sin(t * 5) * 0.08;
      this.ringRed.scale.set(s, s, s);
    }
    if (this.ringSel.visible && !reduced) this.ringSel.rotation.z = t * 0.6;
  }

  dispose() {
    if (this.label) { this.label.material.map.dispose(); this.label.material.dispose(); }
    this.bubble.material.dispose();
    for (const g of this._geos) g.dispose(); // hình gộp riêng từng chibi (geometry chung qua geo() thì không)
    this._geos = [];
    if (this.mesh && this.mesh.skeleton) this.mesh.skeleton.dispose(); // bone texture riêng
    _live.delete(this);
  }
}

function easeOutBack(x) { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2); }
let _unit = null;
function UNIT() { if (!_unit) _unit = new THREE.BoxGeometry(1, 1, 1); return _unit; }
