/* Văn phòng 3D — tiện ích dùng chung: băm id, số ngẫu nhiên có hạt giống, bộ đệm vật liệu/hình khối, chữ trên CanvasTexture.
 * Chữ của agent (tên, task) CHỈ được vẽ bằng fillText lên canvas — không bao giờ thành HTML. */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';

// FNV-1a 32 bit — màu/kiểu tóc ổn định theo id agent.
export function hashStr(s) {
  let h = 0x811c9dc5;
  s = String(s || '');
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return h >>> 0;
}

// mulberry32 — số ngẫu nhiên tất định (bố cục đồ đạc không nhảy mỗi lần tải lại).
export function seeded(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const TEAM_COLORS = ['#e07a5f', '#4f8fc0', '#7fb069', '#e9b44c', '#9c89b8', '#ef8a9a', '#3fb6a8', '#d98c3f'];
export const NO_TEAM_COLOR = '#a39e93';

const _mats = new Map();
// Vật liệu Lambert dùng chung theo màu (rẻ, hợp phong cách low-poly).
export function mat(color) {
  const k = String(color);
  let m = _mats.get(k);
  if (!m) { m = new THREE.MeshLambertMaterial({ color }); _mats.set(k, m); }
  return m;
}

const _basic = new Map();
export function basic(color, opts) {
  const k = color + JSON.stringify(opts || {});
  let m = _basic.get(k);
  if (!m) { m = new THREE.MeshBasicMaterial(Object.assign({ color }, opts || {})); _basic.set(k, m); }
  return m;
}

const _geo = new Map();
export function geo(key, make) {
  let g = _geo.get(key);
  if (!g) { g = make(); _geo.set(key, g); }
  return g;
}
const UNIT_BOX = new THREE.BoxGeometry(1, 1, 1);

// Hộp đặt theo tâm đáy (x, y0, z) — y0 là mặt dưới, tiện xếp đồ lên sàn/bàn.
export function box(parent, w, h, d, color, x, y0, z, material) {
  const m = new THREE.Mesh(UNIT_BOX, material || mat(color));
  m.scale.set(w, h, d);
  m.position.set(x, y0 + h / 2, z);
  parent.add(m);
  return m;
}

export function cyl(parent, rt, rb, h, color, x, y0, z, seg) {
  const s = seg || 12;
  const g = geo('cyl' + rt + ':' + rb + ':' + h + ':' + s, () => new THREE.CylinderGeometry(rt, rb, h, s));
  const m = new THREE.Mesh(g, mat(color));
  m.position.set(x, y0 + h / 2, z);
  parent.add(m);
  return m;
}

export function sphere(parent, r, color, x, y, z, seg) {
  const s = seg || 12;
  const g = geo('sph' + r + ':' + s, () => new THREE.SphereGeometry(r, s, Math.max(6, s * 0.75 | 0)));
  const m = new THREE.Mesh(g, mat(color));
  m.position.set(x, y, z);
  parent.add(m);
  return m;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}
export { roundRect };

export const FONT = 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, "Noto Sans", sans-serif';
export const EMOJI_FONT = '"Apple Color Emoji", "Segoe UI Emoji", "Noto Color Emoji", sans-serif';

// Cắt chữ cho vừa bề rộng (thêm "…"). Chỉ đo/vẽ — không diễn giải nội dung.
export function fitText(ctx, text, maxW) {
  let t = String(text == null ? '' : text).replace(/[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁦-⁩]/g, '');
  if (ctx.measureText(t).width <= maxW) return t;
  while (t.length > 1 && ctx.measureText(t + '…').width > maxW) t = t.slice(0, -1);
  return t + '…';
}

function texFromCanvas(c) {
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 2;
  return t;
}

// Nhãn tên (viên thuốc bo tròn, chấm màu team ở đầu).
export function labelSprite(text, dot, opts) {
  const o = opts || {};
  const c = document.createElement('canvas');
  c.width = 320; c.height = 72;
  const ctx = c.getContext('2d');
  ctx.font = '600 30px ' + FONT;
  const t = fitText(ctx, text, 250);
  const tw = Math.ceil(ctx.measureText(t).width);
  const w = Math.min(316, tw + (dot ? 64 : 40));
  const x0 = (c.width - w) / 2;
  ctx.fillStyle = o.bg || 'rgba(255,253,248,0.92)';
  roundRect(ctx, x0, 8, w, 54, 27); ctx.fill();
  ctx.strokeStyle = o.stroke || 'rgba(60,50,30,0.18)'; ctx.lineWidth = 2; ctx.stroke();
  let tx = x0 + 20;
  if (dot) { ctx.fillStyle = dot; ctx.beginPath(); ctx.arc(x0 + 24, 35, 9, 0, Math.PI * 2); ctx.fill(); tx = x0 + 42; }
  ctx.fillStyle = o.fg || '#2d2a24';
  ctx.textBaseline = 'middle';
  ctx.fillText(t, tx, 36);
  const sm = new THREE.SpriteMaterial({ map: texFromCanvas(c), transparent: true, depthWrite: false, depthTest: o.depthTest !== false });
  const s = new THREE.Sprite(sm);
  const k = o.scale || 1;
  s.scale.set(1.6 * k, 0.36 * k, 1);
  s.userData.base = [1.6 * k, 0.36 * k];
  s.renderOrder = 5;
  return s;
}

// Bong bóng emoji — dùng lại texture theo emoji.
const _bubbleTex = new Map();
export function bubbleTexture(emoji, tone) {
  const k = emoji + '|' + (tone || '');
  let t = _bubbleTex.get(k);
  if (t) return t;
  const c = document.createElement('canvas');
  c.width = 128; c.height = 128;
  const ctx = c.getContext('2d');
  ctx.fillStyle = tone === 'danger' ? '#fde2dc' : tone === 'ask' ? '#efe7fb' : '#fffdf8';
  ctx.strokeStyle = tone === 'danger' ? '#c4483a' : tone === 'ask' ? '#7b5ea7' : 'rgba(60,50,30,0.35)';
  ctx.lineWidth = 5;
  ctx.beginPath(); ctx.arc(64, 56, 46, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
  // đuôi bong bóng
  ctx.beginPath(); ctx.moveTo(52, 98); ctx.lineTo(64, 122); ctx.lineTo(74, 98); ctx.closePath(); ctx.fill();
  ctx.beginPath(); ctx.moveTo(52, 99); ctx.lineTo(64, 122); ctx.lineTo(74, 99); ctx.stroke();
  ctx.fillStyle = ctx.fillStyle; ctx.fillRect(50, 90, 26, 10);
  ctx.font = '54px ' + EMOJI_FONT;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillStyle = '#222';
  ctx.fillText(emoji, 64, 60);
  t = texFromCanvas(c);
  _bubbleTex.set(k, t);
  return t;
}

// Tấm chữ phẳng (biển phòng, tên team) — vẽ một lần.
export function textCanvas(w, h, draw) {
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  draw(c.getContext('2d'), w, h);
  return texFromCanvas(c);
}

export function lerpAngle(a, b, t) {
  let d = b - a;
  while (d > Math.PI) d -= Math.PI * 2;
  while (d < -Math.PI) d += Math.PI * 2;
  return a + d * t;
}

// Nhãn không bé quá khi camera ở xa: nhân hệ số f (≥ 1) vào kích thước gốc.
export function scaleLabel(s, f) {
  const b = s.userData.base;
  if (b && s.userData.f !== f) { s.userData.f = f; s.scale.set(b[0] * f, b[1] * f, 1); }
}
