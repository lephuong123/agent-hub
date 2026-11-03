/* Văn phòng 3D — nhân vật chibi kiểu tượng vinyl (designer toy), dựng thủ tục từ hình khối (đầu to, tay chân ngắn có khớp).
 * Mô hình nhìn về +z khi rotation.y = 0. Tư thế = góc đích cho từng khớp, nội suy mượt + dao động cộng thêm (đi, gõ phím…).
 * Mỗi khớp chuyển động = 1 Mesh gộp nhiều chi tiết, màu nằm ở vertex color (docs/dev-notes/office-3d-visual.md §9):
 * chân trái/phải, thân, tay trái/phải, đầu (sọ + tai + tóc + má + miệng + lông mày), cặp mắt → 7 draw call + bóng blob + nhãn. */
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

// Vật liệu dùng chung (một lần cho mọi chibi). Màu từng chi tiết nằm ở vertex color → mỗi khớp 1 draw call.
let _mats = null;
function mats() {
  if (_mats) return _mats;
  const vinyl = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.6, metalness: 0 });
  // viền sáng nhẹ ở mép (kiểu nhựa vinyl dưới đèn softbox) — rẻ, không thêm draw call
  vinyl.onBeforeCompile = (sh) => {
    sh.fragmentShader = sh.fragmentShader.replace('#include <opaque_fragment>',
      'outgoingLight += vec3(1.0, 0.96, 0.9) * pow(1.0 - saturate(dot(normal, normalize(vViewPosition))), 3.0) * 0.2;\n#include <opaque_fragment>');
  };
  vinyl.customProgramCacheKey = () => 'cfh-vinyl-rim';
  const eye = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.15, metalness: 0 });
  _mats = { vinyl, eye };
  return _mats;
}

// Bóng blob dưới chân: radial đen 0.45 → 0, dùng chung.
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

function shade(hex, k) {
  const c = new THREE.Color(hex);
  const hsl = {}; c.getHSL(hsl);
  c.setHSL(hsl.h, Math.min(1, hsl.s * (0.85 + 0.3 * k)), Math.max(0.18, Math.min(0.8, hsl.l + (k - 0.5) * 0.18)));
  return '#' + c.getHexString();
}
function mix(a, b, k) { return '#' + new THREE.Color(a).lerp(new THREE.Color(b), k).getHexString(); }
function darker(hex, k) { const c = new THREE.Color(hex); c.multiplyScalar(k); return '#' + c.getHexString(); }

function capsule(r, len) { return geo('cap' + r + ':' + len, () => new THREE.CapsuleGeometry(r, len, 4, 12)); }
function hairCap(r, theta) { return geo('hair' + r + ':' + theta, () => new THREE.SphereGeometry(r, 24, 14, 0, Math.PI * 2, 0, theta)); }
const sph = (r, ws, hs) => geo('vs' + r + ':' + ws + ':' + hs, () => new THREE.SphereGeometry(r, ws, hs));

// thân: tiện xoay profile bo (hông 0.16 → eo 0.14 → vai 0.17 → cổ), cao 0.3, gốc y = 0
function torsoGeo() {
  return geo('torsoLathe', () => {
    const P = [[0, 0], [0.1, 0.002], [0.148, 0.018], [0.16, 0.05], [0.152, 0.1], [0.142, 0.145], [0.152, 0.2], [0.168, 0.245],
      [0.166, 0.275], [0.14, 0.3], [0.09, 0.318], [0.05, 0.324], [0, 0.326]];
    return new THREE.LatheGeometry(P.map(([x, y]) => new THREE.Vector2(x, y)), 18);
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

// Gộp các chi tiết [{ g: geometry, m: Matrix4, c: màu }] thành 1 BufferGeometry non-indexed: position, normal, color.
function mergeColored(parts) {
  const list = [];
  let n = 0;
  for (const p of parts) {
    const g = p.g.index ? p.g.toNonIndexed() : p.g.clone();
    g.applyMatrix4(p.m);
    list.push([g, new THREE.Color(p.c)]);
    n += g.attributes.position.count;
  }
  const pos = new Float32Array(n * 3), nor = new Float32Array(n * 3), col = new Float32Array(n * 3);
  let o = 0;
  for (const [g, c] of list) {
    const cnt = g.attributes.position.count;
    pos.set(g.attributes.position.array, o * 3);
    nor.set(g.attributes.normal.array, o * 3);
    for (let i = 0; i < cnt; i++) { col[(o + i) * 3] = c.r; col[(o + i) * 3 + 1] = c.g; col[(o + i) * 3 + 2] = c.b; }
    o += cnt;
    g.dispose();
  }
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  out.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  out.setAttribute('color', new THREE.BufferAttribute(col, 3));
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
    const { vinyl, eye: eyeMat } = mats();
    this._geos = [];
    const mesh = (parts, material, recv) => {
      const g = mergeColored(parts);
      this._geos.push(g);
      const m = new THREE.Mesh(g, material || vinyl);
      m.receiveShadow = !!recv;
      return m;
    };

    const g = this.group = new THREE.Group();
    g.userData.actorId = opts.id;
    const body = this.body = new THREE.Group();
    g.add(body);

    // bóng blob dưới chân (không dùng shadow map)
    const sh = new THREE.Mesh(geo('blob', () => new THREE.PlaneGeometry(BLOB_R * 2, BLOB_R * 2)), blobMat());
    sh.rotation.x = -Math.PI / 2; sh.position.y = 0.006; sh.renderOrder = 1; g.add(sh);
    this.shadow = sh;

    // chân: ống quần + giày bo + đế sáng
    const mkLeg = (x) => {
      const p = new THREE.Group(); p.position.set(x, HIP_Y, 0);
      p.add(mesh([
        { g: capsule(0.075, 0.1), m: M(0, -0.11, 0), c: pants },
        { g: sph(0.08, 16, 10), m: M(0, -0.205, 0.035, 0, 0, 0, 1, 0.55, 1.4), c: SHOE },
        { g: geo('sole', () => new THREE.CylinderGeometry(0.079, 0.079, 0.018, 16)), m: M(0, -0.247, 0.035, 0, 0, 0, 1, 1, 1.4), c: SOLE },
      ]));
      body.add(p); return p;
    };
    this.legL = mkLeg(-0.09); this.legR = mkLeg(0.09);

    // thân: áo (lathe) + cạp quần + cổ áo; điều phối viên: vest + sơ mi chữ V + cà vạt
    const top = this.isManager ? VEST : shirt;
    const tparts = [
      { g: torsoGeo(), m: M(0, 0.27, 0, 0, 0, 0, 1, 1, 0.85), c: top },
      { g: geo('waist', () => new THREE.CylinderGeometry(0.158, 0.163, 0.07, 18)), m: M(0, 0.29, 0, 0, 0, 0, 1, 1, 0.85), c: pants },
    ];
    if (this.isManager) {
      tparts.push({ g: geo('vee', () => new THREE.ConeGeometry(0.07, 0.16, 3)), m: M(0, 0.52, 0.128, Math.PI, 0, 0, 1, 1, 0.5), c: '#ffffff' });
      tparts.push({ g: geo('tie', () => new THREE.ConeGeometry(0.03, 0.18, 4)), m: M(0, 0.47, 0.142, Math.PI, 0, 0, 1, 1, 0.5), c: '#c8413a' });
      tparts.push({ g: geo('knot', () => new THREE.SphereGeometry(0.022, 8, 6)), m: M(0, 0.565, 0.13), c: '#c8413a' });
    } else {
      tparts.push({ g: geo('collar', () => new THREE.TorusGeometry(0.105, 0.02, 8, 24)), m: M(0, 0.585, 0, Math.PI / 2, 0, 0, 1, 0.85, 1), c: '#f6f3ee' });
    }
    const torso = mesh(tparts, vinyl, true);
    body.add(torso);

    // tay: tay áo + bàn tay hơi dẹt
    const mkArm = (x) => {
      const p = new THREE.Group(); p.position.set(x, 0.53, 0);
      p.add(mesh([
        { g: capsule(0.056, 0.115), m: M(0, -0.1, 0), c: top },
        { g: sph(0.06, 14, 10), m: M(0, -0.222, 0, 0, 0, 0, 0.82, 1, 0.95), c: skin },
      ]));
      body.add(p); return p;
    };
    this.armL = mkArm(-0.2); this.armR = mkArm(0.2);
    // cốc cà phê và quyển sách gắn vào tay phải (ẩn mặc định)
    this.cup = new THREE.Group();
    this.cup.add(new THREE.Mesh(geo('cupM', () => mergeColored([
      { g: new THREE.CylinderGeometry(0.045, 0.038, 0.09, 12), m: M(0, 0, 0), c: '#ffffff' },
      { g: new THREE.CircleGeometry(0.04, 12), m: M(0, 0.046, 0, -Math.PI / 2, 0, 0), c: '#6b4226' },
      { g: new THREE.TorusGeometry(0.025, 0.008, 6, 10), m: M(0.048, 0, 0), c: '#ffffff' },
    ])), vinyl));
    this.cup.position.set(0, -0.24, 0.05); this.cup.visible = false; this.armR.add(this.cup);
    this.book = new THREE.Mesh(UNIT(), mat(['#c8413a', '#4f8fc0', '#7fb069', '#e9b44c'][h % 4]));
    this.book.scale.set(0.2, 0.03, 0.15); this.book.position.set(-0.08, -0.25, 0.06); this.book.visible = false; this.armR.add(this.book);
    // điện thoại (rảnh ngồi bàn xem điện thoại — tư thế sitPhone)
    this.phone = new THREE.Mesh(UNIT(), mat('#2b2d33'));
    this.phone.scale.set(0.07, 0.012, 0.13); this.phone.position.set(-0.04, -0.25, 0.05); this.phone.visible = false; this.armR.add(this.phone);

    // đầu: sọ + tai + má hồng + miệng + lông mày + tóc (+ tai nghe điều phối viên) → 1 mesh
    const head = this.head = new THREE.Group(); head.position.set(0, 0.6, 0); body.add(head);
    const brow = mix(darker(hairC, 0.6), '#2a1d17', 0.45);
    const hp = [
      { g: sph(0.3, 28, 20), m: M(0, SKULL_Y, 0, 0, 0, 0, SA / 0.3, SB / 0.3, SC / 0.3), c: skin },
      { g: geo('mouth', () => new THREE.TorusGeometry(0.028, 0.008, 6, 12, Math.PI)), m: onSkull(0, -0.15, 1, 0.002, Math.PI), c: '#7a3b2e' },
    ];
    for (const s of [-1, 1]) {
      hp.push({ g: sph(0.055, 10, 8), m: M(s * 0.292, SKULL_Y - 0.03, -0.01, 0, 0, 0, 0.45, 0.9, 0.75), c: skin }); // tai
      hp.push({ g: geo('blush', () => new THREE.CircleGeometry(0.048, 16)), m: onSkull(s * 0.175, -0.1, 0.25, 0.003, 0, 1, 0.68, 1), c: mix(skin, '#f39a9a', 0.4) });
      hp.push({ g: geo('brow', () => new THREE.BoxGeometry(0.068, 0.017, 0.03)), m: onSkull(s * 0.11, 0.042, 0.26, 0.006, s * -0.14), c: brow });
    }
    // tóc: chỏm cầu nghiêng ra sau (che đỉnh + gáy), mái trước, lọn mái dẹt (bớt "mũ bảo hiểm")
    hp.push({ g: hairCap(0.318, style === 'bob' ? 2.05 : 1.8), m: M(0, SKULL_Y + 0.01, 0, -0.6, 0, 0, 1, 0.95, 0.97), c: hairC });
    hp.push({ g: hairCap(0.322, 0.75), m: M(0, SKULL_Y + 0.01, 0, 0.35, 0, 0, 1, 0.95, 0.97), c: hairC });
    const locks = style === 'spiky' ? [[0, 0.3, 0.95, 0]] : [[-0.17, 0.31, 0.9, 0.35], [0.0, 0.35, 0.95, 0], [0.18, 0.31, 0.9, -0.35]];
    for (const [x, y, z, rz] of locks) {
      hp.push({ g: sph(0.085, 12, 8), m: onSkull(x, y, z, 0.012, rz, 1.05, 0.72, 0.32), c: hairC });
    }
    if (style === 'bob') for (const s of [-1, 1]) hp.push({ g: sph(0.1, 12, 10), m: M(s * 0.27, SKULL_Y - 0.08, -0.04, 0, 0, s * 0.15, 0.5, 1.15, 0.95), c: hairC });
    if (style === 'pony') {
      hp.push({ g: capsule(0.08, 0.2), m: M(0, 0.32, -0.36, 0.55, 0, 0), c: hairC });
      hp.push({ g: geo('tie2', () => new THREE.TorusGeometry(0.06, 0.018, 6, 14)), m: M(0, 0.4, -0.3, 0.55 + Math.PI / 2, 0, 0), c: '#e07a5f' });
    } else if (style === 'buns') {
      for (const sx of [-0.21, 0.21]) hp.push({ g: sph(0.1, 14, 10), m: M(sx, 0.52, -0.05), c: hairC });
    } else if (style === 'spiky') {
      const cg = geo('spike', () => new THREE.ConeGeometry(0.07, 0.17, 8));
      for (let i = 0; i < 5; i++) {
        const a = (i - 2) * 0.35;
        hp.push({ g: cg, m: M(Math.sin(a) * 0.2, 0.55 + Math.cos(a) * 0.04, -0.04 - Math.abs(a) * 0.05, -0.25, 0, -a * 0.9), c: hairC });
      }
    } else if (style === 'ahoge') {
      hp.push({ g: geo('ahoge', () => new THREE.TorusGeometry(0.07, 0.016, 6, 12, Math.PI * 1.2)), m: M(0.02, 0.66, 0, 0, Math.PI / 2, 0), c: hairC });
    }
    if (this.isManager) {
      // tai nghe: vòng qua đầu + chụp tai + mic
      hp.push({ g: geo('band', () => new THREE.TorusGeometry(0.33, 0.022, 8, 24, Math.PI)), m: M(0, 0.3, 0), c: GEAR });
      for (const sx of [-0.32, 0.32]) hp.push({ g: geo('ear', () => new THREE.CylinderGeometry(0.075, 0.075, 0.05, 16)), m: M(sx, 0.28, 0, 0, 0, Math.PI / 2), c: GEAR });
      hp.push({ g: capsule(0.012, 0.2), m: M(0.25, 0.2, 0.14, 1.3, 0, 0.6), c: GEAR });
      hp.push({ g: geo('mictip', () => new THREE.SphereGeometry(0.025, 8, 6)), m: M(0.17, 0.15, 0.24), c: '#c8413a' });
    }
    head.add(mesh(hp, vinyl, true));

    // mắt: một nhóm (chớp = scale.y), hai mắt gộp 1 mesh: lòng đen nâu sẫm + con ngươi + 2 điểm sáng
    const eyeY = onSkull(0.105, -0.04, 0.265, 0).elements[13];
    const ep = [];
    for (const s of [-1, 1]) {
      const base = onSkull(s * 0.105, -0.04, 0.265, -0.004);
      base.elements[13] -= eyeY; // về gốc nhóm mắt
      const at = (x, y, z, sx, sy, sz) => base.clone().multiply(M(x, y, z, 0, 0, 0, sx, sy, sz));
      ep.push({ g: sph(0.05, 14, 12), m: at(0, 0, 0, 0.85, 1.15, 0.45), c: '#3a2519' });
      ep.push({ g: sph(0.03, 10, 8), m: at(0, -0.008, 0.012, 0.85, 1.1, 0.45), c: '#120c09' });
      ep.push({ g: sph(0.016, 8, 6), m: at(0.014, 0.024, 0.02, 1, 1, 0.5), c: '#ffffff' });
      ep.push({ g: sph(0.008, 6, 5), m: at(-0.012, -0.022, 0.02, 1, 1, 0.5), c: '#ffffff' });
    }
    const eyes = new THREE.Group(); eyes.position.y = eyeY;
    eyes.add(mesh(ep, eyeMat));
    head.add(eyes);
    this.eyes = [eyes];

    // vòng dưới chân: đỏ = chờ bạn duyệt; cam = đang chọn
    const ringO = { transparent: true, depthWrite: false, side: THREE.DoubleSide, toneMapped: false, fog: false };
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
  }
}

function easeOutBack(x) { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2); }
let _unit = null;
function UNIT() { if (!_unit) _unit = new THREE.BoxGeometry(1, 1, 1); return _unit; }
