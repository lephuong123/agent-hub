/* Văn phòng 3D — vật liệu, texture thủ tục và gộp hình tĩnh (docs/dev-notes/office-3d-visual.md §4–6).
 * Mọi đồ tĩnh của World đi qua Batch: mỗi "kind" vật liệu = MỘT Mesh gộp (màu từng chi tiết nằm ở vertex color) → vài chục draw call
 * cho cả văn phòng thay vì ~1 500. Texture vẽ bằng canvas 2D (chỉ fillRect/arc/ellipse/stroke/gradient/fillText — chạy được với
 * canvas giả của tests/office_*_check.mjs), dùng chung giữa các lần dựng World (World KHÔNG dispose texture/vật liệu ở đây).
 * Tier 'high' = MeshStandardMaterial (+ env map + shadow do office.js bật); 'low' = MeshLambertMaterial cùng map/vertexColors. */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';

let tier = 'high', aniso = 4;
const _tex = new Map(), _soft = new Map(), _mat = new Map(), _geo = new Map(), _flat = new WeakMap();

export function setTier(t, opts) {
  tier = t === 'low' ? 'low' : 'high';
  if (opts && opts.aniso) aniso = Math.max(1, Math.min(8, opts.aniso));
  for (const tx of _tex.values()) { tx.anisotropy = tier === 'high' ? aniso : 1; tx.needsUpdate = true; }
  for (const m of _mat.values()) m.dispose();
  _mat.clear();
}
export function getTier() { return tier; }

// mulberry32 — texture tất định (không import util.js: module này chỉ phụ thuộc three)
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function canvas(w, h) { const c = document.createElement('canvas'); c.width = w; c.height = h; return c; }
function hsl(hex, dl, dh) {
  const c = new THREE.Color(hex), o = {};
  c.getHSL(o);
  return '#' + c.setHSL((o.h + (dh || 0) / 360 + 1) % 1, o.s, Math.min(1, Math.max(0, o.l * (1 + dl)))).getHexString();
}

// vân gỗ: các đường sóng ngang trong khung (x, y, w, h); vẽ lặp ±W để liền mạch khi RepeatWrapping
function grain(ctx, r, x, y, w, h, n, alpha, W) {
  for (let i = 0; i < n; i++) {
    const gy = y + 3 + r() * (h - 6), amp = 0.6 + r() * 1.8, f = 0.004 + r() * 0.012, ph = r() * 6;
    ctx.strokeStyle = `rgba(90,60,30,${(alpha[0] + r() * (alpha[1] - alpha[0])).toFixed(3)})`;
    ctx.lineWidth = 1 + Math.floor(r() * 2);
    for (const off of W ? [-W, 0, W] : [0]) {
      ctx.beginPath();
      for (let s = 0; s <= w; s += 8) {
        const px = x + s + off, py = gy + Math.sin(s * f * 6.283 + ph) * amp;
        if (s === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.stroke();
    }
  }
}

const PAINT = {
  // sàn sồi: 8 hàng ván 25 cm, tấm 0,8–1,4 m lệch mối, tint từng tấm, vân sóng, khe, mắt gỗ (1 lần lặp = 2 m)
  oakFloor() {
    const S = 512, c = canvas(S, S), ctx = c.getContext('2d'), r = rng(7);
    ctx.fillStyle = '#c79c6e'; ctx.fillRect(0, 0, S, S);
    let k = 0;
    for (let row = 0; row < 8; row++) {
      const y = row * 64;
      let x = -r() * 300;
      while (x < S) {
        const L = (0.8 + r() * 0.6) * 256, col = hsl('#c79c6e', (r() - 0.5) * 0.12, (r() - 0.5) * 4);
        for (const off of [-S, 0, S]) { ctx.fillStyle = col; ctx.fillRect(x + off, y, L, 64); }
        grain(ctx, r, x, y, L, 64, 4 + Math.floor(r() * 2), [0.06, 0.12], S);
        if (++k % 12 === 0) {
          const kx = x + 30 + r() * (L - 60), ky = y + 14 + r() * 36;
          for (const off of [-S, 0, S]) {
            const gr = ctx.createRadialGradient(kx + off, ky, 0, kx + off, ky, 7);
            gr.addColorStop(0, 'rgba(80,50,25,0.55)'); gr.addColorStop(1, 'rgba(80,50,25,0)');
            ctx.fillStyle = gr; ctx.beginPath(); ctx.ellipse(kx + off, ky, 9, 5, 0, 0, Math.PI * 2); ctx.fill();
          }
        }
        ctx.fillStyle = 'rgba(60,40,20,0.35)';
        for (const off of [-S, 0, S]) ctx.fillRect(x + off, y, 1, 64);
        x += L;
      }
      ctx.fillStyle = 'rgba(60,40,20,0.35)'; ctx.fillRect(0, y, S, 2);
      ctx.fillStyle = 'rgba(255,240,215,0.10)'; ctx.fillRect(0, y + 2, S, 1);
    }
    return c;
  },
  // gỗ sáng (laminate) — nền trắng: màu thật nằm ở vertex color, texture chỉ mang vân
  woodLight() {
    const S = 256, c = canvas(S, S), ctx = c.getContext('2d'), r = rng(11);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, S, S);
    for (let y = 0; y < S; y += 32) { ctx.fillStyle = `rgba(120,90,50,${(r() * 0.04).toFixed(3)})`; ctx.fillRect(0, y, S, 32); }
    grain(ctx, r, 0, 0, S, S, 26, [0.03, 0.07], S);
    return c;
  },
  walnut() {
    const S = 256, c = canvas(S, S), ctx = c.getContext('2d'), r = rng(13);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, S, S);
    for (let y = 0; y < S; y += 32) { ctx.fillStyle = `rgba(60,35,15,${(r() * 0.12).toFixed(3)})`; ctx.fillRect(0, y, S, 32); }
    grain(ctx, r, 0, 0, S, S, 34, [0.1, 0.22], S);
    return c;
  },
  plaster() {
    const S = 256, c = canvas(S, S), ctx = c.getContext('2d'), r = rng(17);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, S, S);
    for (let i = 0; i < 2; i++) {
      const x = r() * S, y = r() * S, gr = ctx.createRadialGradient(x, y, 0, x, y, 90);
      gr.addColorStop(0, 'rgba(120,100,80,0.03)'); gr.addColorStop(1, 'rgba(120,100,80,0)');
      ctx.fillStyle = gr; ctx.fillRect(0, 0, S, S);
    }
    for (let i = 0; i < 3000; i++) {
      ctx.fillStyle = r() < 0.5 ? 'rgba(0,0,0,0.03)' : 'rgba(255,255,255,0.5)';
      ctx.fillRect(Math.floor(r() * S), Math.floor(r() * S), 1, 1);
    }
    return c;
  },
  carpet() {
    const S = 256, c = canvas(S, S), ctx = c.getContext('2d'), r = rng(19);
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, S, S);
    for (let i = 0; i < 6000; i++) {
      ctx.fillStyle = r() < 0.5 ? 'rgba(0,0,0,0.08)' : 'rgba(255,255,255,0.08)';
      ctx.fillRect(Math.floor(r() * S), Math.floor(r() * S), 1, 1);
    }
    ctx.fillStyle = 'rgba(0,0,0,0.08)';
    for (let i = 0; i < S; i += 128) { ctx.fillRect(i, 0, 1, S); ctx.fillRect(0, i, S, 1); }
    return c;
  },
  fabric() {
    const S = 128, c = canvas(S, S), ctx = c.getContext('2d');
    ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, S, S);
    ctx.lineWidth = 1;
    for (const [a, col] of [[1, 'rgba(0,0,0,0.06)'], [-1, 'rgba(0,0,0,0.04)']]) {
      ctx.strokeStyle = col;
      for (let i = -S; i < 2 * S; i += 4) { ctx.beginPath(); ctx.moveTo(i, 0); ctx.lineTo(i + a * S, S); ctx.stroke(); }
    }
    return c;
  },
  terrazzo() {
    const S = 256, c = canvas(S, S), ctx = c.getContext('2d'), r = rng(23);
    ctx.fillStyle = '#ebe5da'; ctx.fillRect(0, 0, S, S);
    const C = ['#c9b8a6', '#9aa39b', '#d9a088', '#7d7a75'];
    for (let i = 0; i < 400; i++) {
      const x = r() * S, y = r() * S, s = 1 + r() * 2.5;
      ctx.fillStyle = C[Math.floor(r() * C.length)];
      for (const [ox, oy] of [[0, 0], [S, 0], [-S, 0], [0, S], [0, -S]]) { ctx.beginPath(); ctx.ellipse(x + ox, y + oy, s, s * (0.6 + r() * 0.4), r() * 3, 0, Math.PI * 2); ctx.fill(); }
    }
    return c;
  },
  // sàn nâng phòng server: 2×2 tấm 60 cm, 1/4 tấm đục lỗ
  tiles() {
    const S = 256, c = canvas(S, S), ctx = c.getContext('2d');
    ctx.fillStyle = '#9aa1a6'; ctx.fillRect(0, 0, S, S);
    for (let i = 0; i < 2; i++) for (let j = 0; j < 2; j++) {
      ctx.fillStyle = '#c3c9cd'; ctx.fillRect(i * 128 + 1, j * 128 + 1, 126, 126);
      ctx.fillStyle = 'rgba(255,255,255,0.18)'; ctx.fillRect(i * 128 + 1, j * 128 + 1, 126, 2);
      if (i === 1 && j === 0) {
        ctx.fillStyle = 'rgba(70,78,84,0.55)';
        for (let x = 12; x < 120; x += 8) for (let y = 12; y < 120; y += 8) { ctx.beginPath(); ctx.arc(i * 128 + x, j * 128 + y, 1.6, 0, Math.PI * 2); ctx.fill(); }
      }
    }
    return c;
  },
  sky() {
    const c = canvas(256, 128), ctx = c.getContext('2d'), r = rng(29);
    const gr = ctx.createLinearGradient(0, 0, 0, 128);
    gr.addColorStop(0, '#ffffff'); gr.addColorStop(1, '#dfe7ea');
    ctx.fillStyle = gr; ctx.fillRect(0, 0, 256, 128);
    ctx.fillStyle = 'rgba(70,90,110,0.10)';
    for (let x = 0; x < 256;) { const w = 14 + r() * 26, h = 18 + r() * 46; ctx.fillRect(x, 128 - h, w - 3, h); x += w; }
    return c;
  },
  screenCode() {
    const c = canvas(256, 128), ctx = c.getContext('2d'), r = rng(31);
    ctx.fillStyle = '#000000'; ctx.fillRect(0, 0, 256, 128);
    ctx.fillStyle = 'rgba(255,255,255,0.22)'; ctx.fillRect(0, 0, 256, 9);
    const C = ['rgba(255,255,255,0.95)', 'rgba(190,205,220,0.85)', 'rgba(140,160,180,0.8)'];
    for (let i = 0; i < 14; i++) {
      let x = 10 + Math.floor(r() * 4) * 10;
      const y = 14 + i * 8;
      for (let k = 0; k < 1 + Math.floor(r() * 3) && x < 240; k++) {
        const w = 12 + r() * 60;
        ctx.fillStyle = C[Math.floor(r() * 3)]; ctx.fillRect(x, y, Math.min(w, 246 - x), 3);
        x += w + 6;
      }
    }
    return c;
  },
  screenGame() {
    const c = canvas(256, 128), ctx = c.getContext('2d'), r = rng(37);
    const gr = ctx.createLinearGradient(0, 0, 0, 128);
    gr.addColorStop(0, '#202a55'); gr.addColorStop(0.55, '#c65a8c'); gr.addColorStop(0.56, '#141022'); gr.addColorStop(1, '#30245a');
    ctx.fillStyle = gr; ctx.fillRect(0, 0, 256, 128);
    ctx.fillStyle = 'rgba(255,230,160,0.95)'; ctx.beginPath(); ctx.arc(128, 70, 20, Math.PI, 0); ctx.fill();
    ctx.strokeStyle = 'rgba(120,240,255,0.7)'; ctx.lineWidth = 1;
    for (let i = 0; i < 7; i++) { ctx.beginPath(); ctx.moveTo(128, 72); ctx.lineTo(-200 + i * 110, 128); ctx.stroke(); }
    for (let i = 0; i < 6; i++) { ctx.fillStyle = ['#7dff5a', '#ff3df2', '#3df5ff'][i % 3]; ctx.fillRect(20 + r() * 210, 20 + r() * 40, 8, 8); }
    return c;
  },
};
const UV_SIZE = { floor: 2, wood: 1, walnut: 1, plaster: 2, fabric: 0.25, carpet: 1, terrazzo: 1, tiles: 1.2 };

export function tex(kind) {
  let t = _tex.get(kind);
  if (t) return t;
  t = new THREE.CanvasTexture(PAINT[kind]());
  t.colorSpace = THREE.SRGBColorSpace;
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.anisotropy = tier === 'high' ? aniso : 1;
  _tex.set(kind, t);
  return t;
}

// texture mềm: 'ao' (bóng tiếp xúc), 'pool' (vũng đèn), 'glow' (dải neon)
export function softTex(kind) {
  let t = _soft.get(kind);
  if (t) return t;
  let c;
  if (kind === 'ao') {
    c = canvas(64, 64); const ctx = c.getContext('2d');
    ctx.fillStyle = 'rgba(255,255,255,0.11)';
    for (let i = 0; i < 10; i++) {
      const m = 3 + i * 2.6, rr = Math.max(2, 14 - i);
      ctx.beginPath();
      ctx.moveTo(m + rr, m); ctx.lineTo(64 - m - rr, m); ctx.arc(64 - m - rr, m + rr, rr, -Math.PI / 2, 0);
      ctx.lineTo(64 - m, 64 - m - rr); ctx.arc(64 - m - rr, 64 - m - rr, rr, 0, Math.PI / 2);
      ctx.lineTo(m + rr, 64 - m); ctx.arc(m + rr, 64 - m - rr, rr, Math.PI / 2, Math.PI);
      ctx.lineTo(m, m + rr); ctx.arc(m + rr, m + rr, rr, Math.PI, Math.PI * 1.5);
      ctx.fill();
    }
  } else if (kind === 'pool') {
    c = canvas(128, 128); const ctx = c.getContext('2d');
    const gr = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    gr.addColorStop(0, 'rgba(255,214,150,0.9)'); gr.addColorStop(0.45, 'rgba(255,214,150,0.4)'); gr.addColorStop(1, 'rgba(255,214,150,0)');
    ctx.fillStyle = gr; ctx.fillRect(0, 0, 128, 128);
  } else {
    c = canvas(64, 16); const ctx = c.getContext('2d');
    const gr = ctx.createLinearGradient(0, 0, 0, 16);
    gr.addColorStop(0, 'rgba(255,255,255,0)'); gr.addColorStop(0.5, 'rgba(255,255,255,1)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gr; ctx.fillRect(0, 0, 64, 16);
  }
  t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  _soft.set(kind, t);
  return t;
}

// ------------------------------------------------------------ vật liệu
const SPEC = {
  wood: { map: 'woodLight', roughness: 0.55 },
  walnut: { map: 'walnut', roughness: 0.5 },
  plaster: { map: 'plaster', roughness: 0.9 },
  paint: { roughness: 0.7 },
  fabric: { map: 'fabric', roughness: 0.95 },
  carpet: { map: 'carpet', roughness: 1, cast: false, offset: true },
  metal: { roughness: 0.4, metalness: 0.6 },
  chrome: { roughness: 0.25, metalness: 1 },
  plastic: { roughness: 0.45 },
  ceramic: { roughness: 0.35 },
  plant: { roughness: 0.65 },
  floor: { map: 'oakFloor', roughness: 0.5, cast: false },
  terrazzo: { map: 'terrazzo', roughness: 0.45, cast: false },
  tiles: { map: 'tiles', roughness: 0.6, cast: false },
  ground: { roughness: 1, cast: false },
};
export const castsShadow = (kind) => !SPEC[kind] || SPEC[kind].cast !== false;

// vật liệu "sáng" theo tier (Standard ↔ Lambert) — cho vật liệu riêng của World (nền đất, bệ, sách, kính)
export function litMaterial(p) {
  const o = Object.assign({}, p);
  if (tier === 'low') { delete o.roughness; delete o.metalness; delete o.envMapIntensity; return new THREE.MeshLambertMaterial(o); }
  return new THREE.MeshStandardMaterial(o);
}

export function surface(kind, opts) {
  const key = kind + (opts ? JSON.stringify(opts) : '');
  let m = _mat.get(key);
  if (m) return m;
  if (kind === 'glass') {
    m = tier === 'low'
      ? new THREE.MeshLambertMaterial({ color: '#cfe6f0', transparent: true, opacity: 0.22, depthWrite: false })
      : new THREE.MeshStandardMaterial({ color: '#d8edf5', roughness: 0.05, metalness: 0, transparent: true, opacity: 0.18, envMapIntensity: 1.2, depthWrite: false });
  } else {
    const s = SPEC[kind] || SPEC.paint;
    const p = Object.assign({ color: '#ffffff', vertexColors: true, roughness: s.roughness, metalness: s.metalness || 0 }, opts || {});
    if (s.map) p.map = tex(s.map);
    if (s.offset) Object.assign(p, { polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 });
    m = litMaterial(p);
  }
  _mat.set(key, m);
  return m;
}

// ------------------------------------------------------------ hình học
// Hộp bo góc (thuật toán như addon RoundedBoxGeometry, viết lại — vendor không có addon). Tâm ở giữa, cache theo mm.
export function roundedBoxGeo(w, h, d, r, seg = 1) {
  const key = 'rb' + [w, h, d, r].map((v) => Math.round(v * 1000)).join('|') + '|' + seg;
  let g = _geo.get(key);
  if (g) return g;
  const n = seg * 2 + 1, hs = 0.5 / n;
  g = new THREE.BoxGeometry(1, 1, 1, n, n, n);
  const p = g.attributes.position, q = g.attributes.normal, v = new THREE.Vector3(), nv = new THREE.Vector3();
  r = Math.max(1e-4, Math.min(r, w / 2 - 1e-4, h / 2 - 1e-4, d / 2 - 1e-4));
  for (let i = 0; i < p.count; i++) {
    v.fromBufferAttribute(p, i);
    nv.set(v.x - Math.sign(v.x) * hs, v.y - Math.sign(v.y) * hs, v.z - Math.sign(v.z) * hs).normalize();
    p.setXYZ(i, Math.sign(v.x) * (w / 2 - r) + nv.x * r, Math.sign(v.y) * (h / 2 - r) + nv.y * r, Math.sign(v.z) * (d / 2 - r) + nv.z * r);
    q.setXYZ(i, nv.x, nv.y, nv.z);
  }
  _geo.set(key, g);
  return g;
}
function cached(key, make) { let g = _geo.get(key); if (!g) { g = make(); _geo.set(key, g); } return g; }
const unitBox = () => cached('box', () => new THREE.BoxGeometry(1, 1, 1));
const flat = (g) => { if (!g.index) return g; let f = _flat.get(g); if (!f) { f = g.toNonIndexed(); _flat.set(g, f); } return f; };

// parts: [{ geo, matrix, color?, uv?: 'box'|'keep', uvSize? }] → BufferGeometry non-indexed (position, normal, uv, color)
export function mergeGeometries(parts) {
  let n = 0;
  const src = parts.map((p) => { const g = flat(p.geo); n += g.attributes.position.count; return g; });
  const pos = new Float32Array(n * 3), nor = new Float32Array(n * 3), uv = new Float32Array(n * 2), col = new Float32Array(n * 3);
  const v = new THREE.Vector3(), nm = new THREE.Matrix3(), c = new THREE.Color(), I = new THREE.Matrix4();
  let o = 0;
  parts.forEach((p, k) => {
    const g = src[k], P = g.attributes.position, N = g.attributes.normal, U = g.attributes.uv, m = p.matrix || I;
    nm.getNormalMatrix(m);
    c.set(p.color == null ? '#ffffff' : p.color);
    const box = p.uv !== 'keep', us = p.uvSize || 1;
    for (let i = 0; i < P.count; i++, o++) {
      v.fromBufferAttribute(P, i).applyMatrix4(m);
      pos[o * 3] = v.x; pos[o * 3 + 1] = v.y; pos[o * 3 + 2] = v.z;
      const px = v.x, py = v.y, pz = v.z;
      if (N) v.fromBufferAttribute(N, i).applyMatrix3(nm).normalize(); else v.set(0, 1, 0);
      nor[o * 3] = v.x; nor[o * 3 + 1] = v.y; nor[o * 3 + 2] = v.z;
      if (box) {
        const ax = Math.abs(v.x), ay = Math.abs(v.y), az = Math.abs(v.z);
        if (ax >= ay && ax >= az) { uv[o * 2] = pz / us; uv[o * 2 + 1] = py / us; }
        else if (ay >= az) { uv[o * 2] = px / us; uv[o * 2 + 1] = pz / us; }
        else { uv[o * 2] = px / us; uv[o * 2 + 1] = py / us; }
      } else if (U) { uv[o * 2] = U.getX(i); uv[o * 2 + 1] = U.getY(i); }
      col[o * 3] = c.r; col[o * 3 + 1] = c.g; col[o * 3 + 2] = c.b;
    }
  });
  const out = new THREE.BufferGeometry();
  out.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  out.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  out.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  out.setAttribute('color', new THREE.BufferAttribute(col, 3));
  out.computeBoundingSphere();
  return out;
}

// Mesh riêng (không gộp) có vertex color — cho occluder, kim đồng hồ, cánh cửa… (vật liệu surface() cần attribute color)
export function solidMesh(geo, kind, color, opts) {
  const o = opts || {};
  const m = new THREE.Mesh(mergeGeometries([{ geo, color, uvSize: o.uvSize || UV_SIZE[kind] || 1 }]), o.material || surface(kind));
  m.castShadow = castsShadow(kind) && o.shadow !== false; m.receiveShadow = true;
  return m;
}

const _m4 = new THREE.Matrix4(), _q = new THREE.Quaternion(), _e = new THREE.Euler(), _p = new THREE.Vector3(), _s = new THREE.Vector3();
function place(x, y, z, sx, sy, sz, o) {
  _e.set(o.rx || 0, o.rotY || o.ry || 0, o.rz || 0);
  _q.setFromEuler(_e);
  const m = new THREE.Matrix4().compose(_p.set(x, y, z), _q, _s.set(sx, sy, sz));
  return o.m ? _m4.copy(o.m).multiply(m).clone() : m;
}

// Gom mesh tĩnh theo vật liệu: mỗi khoá (kind) → 1 Mesh khi build().
export class Batch {
  constructor() { this.parts = new Map(); this.meshes = []; }
  add(geo, matrix, kind, color, opts) {
    const o = opts || {};
    let L = this.parts.get(kind);
    if (!L) { L = []; this.parts.set(kind, L); }
    L.push({ geo, matrix, color, uv: o.uv || 'box', uvSize: o.uvSize || UV_SIZE[kind] || 1 });
    return this;
  }
  // tâm đáy (x, y0, z) như util.box; opts.r > 0 → hộp bo góc; opts.rotY/rx/rz quay quanh tâm hộp; opts.m = ma trận cha
  box(w, h, d, x, y0, z, kind, color, opts) {
    const o = opts || {};
    if (o.r > 0) return this.add(roundedBoxGeo(w, h, d, o.r, o.seg || 1), place(x, y0 + h / 2, z, 1, 1, 1, o), kind, color, o);
    return this.add(unitBox(), place(x, y0 + h / 2, z, w, h, d, o), kind, color, o);
  }
  cyl(rt, rb, h, x, y0, z, kind, color, seg, opts) {
    const s = seg || 12, o = opts || {};
    const g = cached(`cyl${rt}:${rb}:${h}:${s}`, () => new THREE.CylinderGeometry(rt, rb, h, s));
    return this.add(g, place(x, y0 + h / 2, z, 1, 1, 1, o), kind, color, o);
  }
  sphere(r, x, y, z, kind, color, seg, opts) {
    const s = seg || 12, o = opts || {}, k = o.scale || [1, 1, 1];
    const g = cached(`sph${s}:${o.rings || 0}`, () => new THREE.SphereGeometry(1, s, o.rings || Math.max(4, (s * 0.75) | 0)));
    return this.add(g, place(x, y, z, r * k[0], r * k[1], r * k[2], o), kind, color, o);
  }
  // overrides: { kind: material } — cho phần phát sáng (neon, màn, đèn) dùng vật liệu riêng của World
  build(parent, overrides) {
    for (const [kind, parts] of this.parts) {
      const mat = (overrides && overrides[kind]) || surface(kind);
      const mesh = new THREE.Mesh(mergeGeometries(parts), mat);
      const lit = !(overrides && overrides[kind]);
      mesh.castShadow = lit && castsShadow(kind); mesh.receiveShadow = lit;
      mesh.matrixAutoUpdate = false;
      parent.add(mesh);
      this.meshes.push(mesh);
    }
    this.parts.clear();
    return this.meshes;
  }
  dispose() { for (const m of this.meshes) { m.geometry.dispose(); m.removeFromParent(); } this.meshes = []; }
}

// Environment map tự dựng (không HDR ngoài): phòng hộp tối ấm + 4 softbox trần + 1 "cửa sổ" ấm phía bắc → PMREM.
export function makeEnvironment(renderer) {
  const scene = new THREE.Scene(), geos = [], mats = [];
  const add = (geo, mat, x, y, z, rx) => { geos.push(geo); mats.push(mat); const m = new THREE.Mesh(geo, mat); m.position.set(x, y, z); if (rx) m.rotation.x = rx; scene.add(m); return m; };
  add(new THREE.BoxGeometry(20, 8, 20), new THREE.MeshBasicMaterial({ color: '#5d5953', side: THREE.BackSide }), 0, 4, 0);
  add(new THREE.PlaneGeometry(20, 20), new THREE.MeshBasicMaterial({ color: '#7a6650' }), 0, 0.01, 0, -Math.PI / 2);
  const box = new THREE.Color('#ffffff').setScalar(5);
  for (const [x, z] of [[-4, -4], [4, -4], [-4, 4], [4, 4]]) add(new THREE.PlaneGeometry(3, 3), new THREE.MeshBasicMaterial({ color: box }), x, 7.9, z, Math.PI / 2);
  add(new THREE.PlaneGeometry(12, 3), new THREE.MeshBasicMaterial({ color: new THREE.Color('#ffe2b8').multiplyScalar(3) }), 0, 4, -9.9);
  const gen = new THREE.PMREMGenerator(renderer);
  const env = gen.fromScene(scene, 0.04).texture;
  gen.dispose();
  for (const g of geos) g.dispose();
  for (const m of mats) m.dispose();
  return env;
}
