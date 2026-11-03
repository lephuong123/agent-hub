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
  t.anisotropy = 4;
  return t;
}

// Nhãn tên (viên thuốc bo tròn, chấm màu team viền trắng ở đầu). Canvas 2× (640×144) cho nét; bóng đổ mềm vẽ trên canvas.
// Không bị tone mapping/fog làm xỉn (toneMapped/fog false). Hợp đồng: Sprite, userData.base = [1.6k, 0.36k], renderOrder 5.
export function labelSprite(text, dot, opts) {
  const o = opts || {};
  const c = document.createElement('canvas');
  c.width = 640; c.height = 144;
  const ctx = c.getContext('2d');
  ctx.font = '600 52px ' + FONT;
  const t = fitText(ctx, text, 470);
  const tw = Math.ceil(ctx.measureText(t).width);
  const w = Math.min(612, tw + (dot ? 118 : 72));
  const x0 = (c.width - w) / 2, y0 = 22, ph = 92;
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,0.18)'; ctx.shadowBlur = 10; ctx.shadowOffsetY = 3;
  ctx.fillStyle = o.bg || 'rgba(255,253,248,0.94)';
  roundRect(ctx, x0, y0, w, ph, ph / 2); ctx.fill();
  ctx.restore();
  ctx.strokeStyle = o.stroke || 'rgba(60,50,30,0.14)'; ctx.lineWidth = 2; roundRect(ctx, x0, y0, w, ph, ph / 2); ctx.stroke();
  let tx = x0 + 36;
  if (dot) {
    ctx.fillStyle = '#ffffff'; ctx.beginPath(); ctx.arc(x0 + 46, y0 + ph / 2, 19, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = dot; ctx.beginPath(); ctx.arc(x0 + 46, y0 + ph / 2, 14, 0, Math.PI * 2); ctx.fill();
    tx = x0 + 78;
  }
  ctx.fillStyle = o.fg || '#2d2a24';
  ctx.textBaseline = 'middle';
  ctx.fillText(t, tx, y0 + ph / 2 + 2);
  const sm = new THREE.SpriteMaterial({ map: texFromCanvas(c), transparent: true, depthWrite: false, depthTest: o.depthTest !== false,
    toneMapped: false, fog: false });
  const s = new THREE.Sprite(sm);
  const k = o.scale || 1;
  s.scale.set(1.6 * k, 0.36 * k, 1);
  s.userData.base = [1.6 * k, 0.36 * k];
  s.renderOrder = 5;
  return s;
}

// Bong bóng emoji — dùng lại texture theo emoji. Canvas 2× (256), bong bóng bo + đuôi + bóng đổ mềm.
const _bubbleTex = new Map();
export function bubbleTexture(emoji, tone) {
  const k = emoji + '|' + (tone || '');
  let t = _bubbleTex.get(k);
  if (t) return t;
  const c = document.createElement('canvas');
  c.width = 256; c.height = 256;
  const ctx = c.getContext('2d');
  const fill = tone === 'danger' ? '#fde2dc' : tone === 'ask' ? '#efe7fb' : '#fffdf8';
  const edge = tone === 'danger' ? '#c4483a' : tone === 'ask' ? '#7b5ea7' : 'rgba(60,50,30,0.28)';
  const shape = () => {
    ctx.beginPath();
    ctx.arc(128, 110, 88, Math.PI * 0.62, Math.PI * 0.38 + Math.PI * 2, false); // vòng tròn chừa chỗ đuôi ở dưới
    ctx.quadraticCurveTo(140, 214, 128, 236);
    ctx.quadraticCurveTo(116, 214, 128 + 88 * Math.cos(Math.PI * 0.62), 110 + 88 * Math.sin(Math.PI * 0.62));
    ctx.closePath();
  };
  ctx.save();
  ctx.shadowColor = 'rgba(0,0,0,0.2)'; ctx.shadowBlur = 14; ctx.shadowOffsetY = 4;
  ctx.fillStyle = fill; shape(); ctx.fill();
  ctx.restore();
  ctx.strokeStyle = edge; ctx.lineWidth = tone ? 7 : 4; shape(); ctx.stroke();
  ctx.font = '104px ' + EMOJI_FONT;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillStyle = '#222';
  ctx.fillText(emoji, 128, 116);
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

// Nhãn tên tránh chồng: items [{id, x, y, w, h, pri}] = hình chữ nhật trên màn hình (tâm x, y; pixel). Duyệt theo pri giảm dần,
// giữ nhãn không chồng nhãn đã giữ → Set id được hiện. Chỗ đông (cafe) chỉ còn vài tên đọc được thay vì một đám chồng nhau.
export function declutter(items) {
  const kept = [], out = new Set();
  for (const it of [...items].sort((a, b) => b.pri - a.pri)) {
    // vùng chiếm = nhãn + khoảng bong bóng emoji phía trên (≈ 1,2 lần cao nhãn) → chỗ đông (cafe) không còn nhãn đè bong bóng
    if (kept.some((k) => Math.abs(k.x - it.x) * 2 < k.w + it.w && it.y - it.h * 1.7 < k.y + k.h / 2 && k.y - k.h * 1.7 < it.y + it.h / 2)) continue;
    kept.push(it); out.add(it.id);
  }
  return out;
}
