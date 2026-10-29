/* Văn phòng 3D — bảng tương tác vẽ trên canvas (giai đoạn 2: duyệt, câu hỏi, chat, đăng xuất ngay trong cảnh).
 * Kiểu "immediate mode": mỗi lần vẽ, hàm build(ui) vẽ + đăng ký vùng bấm (nút, ô nhập, hộp cuộn, ô tích). Raycast lên mặt phẳng → uv → px → điều khiển.
 * Mỗi điều khiển có bản sao DOM trong khối sr-only (nút <button>, ô nhập <textarea> THẬT): bàn phím Tab/Enter/Space, trình đọc màn hình và
 * bộ gõ tiếng Việt (IME) đều chạy trên phần tử DOM thật; canvas chỉ vẽ lại theo sự kiện `input` (không chặn composition).
 * Chữ của agent chỉ vẽ bằng fillText; trong DOM chỉ textContent / value. */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { FONT, roundRect } from './util.js';

export const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
const C = {
  bg: '#fffdf8', line: '#e2dccd', text: '#1f1e1d', muted: '#73726c', accent: '#c96442', danger: '#c4483a', ok: '#4d7c43',
  ghost: '#efebe1', focus: '#2f6fd6', warnBg: '#fde2dc', code: '#f3efe6',
};
export { C as COLORS };

// Chặt chữ theo bề rộng (giữ xuống dòng gốc; từ quá dài cắt theo ký tự) — dùng cho lệnh/thao tác đầy đủ và tin nhắn.
export function wrapLines(ctx, text, maxW, maxLines) {
  const out = [];
  const cap = maxLines || 4000;
  for (const raw of String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n')) {
    let line = '';
    for (const word of raw.split(/(\s+)/)) {
      if (!word) continue;
      const t = line + word;
      if (ctx.measureText(t).width <= maxW) { line = t; continue; }
      if (line.trim()) { out.push(line.replace(/\s+$/, '')); line = ''; if (out.length >= cap) return out; }
      let w = word.replace(/^\s+/, '');
      while (ctx.measureText(w).width > maxW && w.length > 1) {
        let n = w.length;
        while (n > 1 && ctx.measureText(w.slice(0, n)).width > maxW) n = Math.max(1, Math.floor(n * 0.8));
        while (n < w.length && ctx.measureText(w.slice(0, n + 1)).width <= maxW) n++;
        out.push(w.slice(0, n)); w = w.slice(n);
        if (out.length >= cap) return out;
      }
      line = w;
    }
    out.push(line);
    if (out.length >= cap) return out;
  }
  return out;
}

// Bỏ ký tự điều khiển/vô hình khi VẼ (dữ liệu agent) — ký tự ẩn đã được cảnh báo riêng qua hidden_chars.
export function clean(s) {
  return String(s == null ? '' : s).replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏‪-‮⁠-⁩﻿]/g, '·');
}

export class Panel {
  /**
   * opts: {pxW, pxH, title, srRoot, modal, build(ui), onEscape(), role}
   */
  constructor(opts) {
    this.opts = opts;
    this.canvas = document.createElement('canvas');
    this.tex = new THREE.CanvasTexture(this.canvas);
    this.tex.colorSpace = THREE.SRGBColorSpace;
    this.tex.anisotropy = 4;
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
      map: this.tex, transparent: true, toneMapped: false, depthTest: !opts.modal, depthWrite: !opts.modal,
    }));
    this.mesh.renderOrder = opts.modal ? 1001 : 0;
    this.mesh.userData.panel = this;
    this.setSize(opts.pxW, opts.pxH);
    this.controls = [];
    this.focusKey = null; this.hoverKey = null; this.busy = false; this.scroll = {}; this.dirty = true;
    this.state = {};
    this.sr = document.createElement('section');
    if (opts.modal) { this.sr.setAttribute('role', 'dialog'); this.sr.setAttribute('aria-modal', 'true'); }
    this.sr.setAttribute('aria-label', opts.title || 'Bảng');
    opts.srRoot.appendChild(this.sr);
    this.els = new Map(); // key → phần tử DOM bản sao
    this.sr.addEventListener('keydown', (e) => this._trap(e));
  }

  setSize(pxW, pxH) {
    const changed = this.W && (this.W !== pxW || this.H !== pxH);
    this.canvas.width = pxW; this.canvas.height = pxH;
    if (changed) {
      // kích thước canvas đổi → texture mới (WebGL không tự cấp phát lại cho CanvasTexture đã tải lên GPU)
      this.tex.dispose();
      this.tex = new THREE.CanvasTexture(this.canvas);
      this.tex.colorSpace = THREE.SRGBColorSpace; this.tex.anisotropy = 4;
      this.mesh.material.map = this.tex; this.mesh.material.needsUpdate = true;
    }
    this.W = pxW; this.H = pxH;
    this.dirty = true;
  }

  get(key) { return this.els.get(key); }
  value(key) { const e = this.els.get(key); return e ? e.value : ''; }
  checked(key) { const e = this.els.get(key); return !!(e && e.checked); }

  draw() {
    if (!this.dirty) return;
    this.dirty = false;
    const ctx = this.canvas.getContext('2d');
    ctx.clearRect(0, 0, this.W, this.H);
    const ui = new UI(this, ctx);
    this.opts.build(ui);
    this.controls = ui.controls;
    this._syncSr(ui.items);
    this.tex.needsUpdate = true;
  }

  // ---------------------------------------------------------------- bản sao DOM (sr-only)
  _syncSr(items) {
    const keep = new Set();
    let prev = null;
    for (const it of items) {
      keep.add(it.key);
      let el = this.els.get(it.key);
      if (!el || el.dataset.kind !== it.type) {
        if (el) el.remove();
        el = this._make(it);
        this.els.set(it.key, el);
      }
      this._update(el, it);
      // giữ đúng thứ tự Tab (chỉ di chuyển khi lệch — di chuyển phần tử đang focus sẽ làm mất focus)
      const want = prev ? prev.nextSibling : this.sr.firstChild;
      if (want !== el) this.sr.insertBefore(el, want);
      prev = el;
    }
    for (const [k, el] of this.els) if (!keep.has(k)) { el.remove(); this.els.delete(k); }
  }

  _make(it) {
    let el;
    if (it.type === 'button') {
      el = document.createElement('button'); el.type = 'button';
      el.addEventListener('click', () => this.activate(it.key));
    } else if (it.type === 'field') {
      el = document.createElement('textarea');
      el.rows = it.rows || 1;
      el.setAttribute('autocomplete', 'off');
      el.addEventListener('input', () => { this.dirty = true; });
      el.addEventListener('keydown', (e) => { const c = this._ctl(it.key); if (c && c.onKey) c.onKey(e); });
    } else if (it.type === 'check') {
      el = document.createElement('input'); el.type = 'checkbox';
      el.addEventListener('change', () => { this.dirty = true; });
    } else if (it.type === 'scroll') {
      el = document.createElement('div'); el.tabIndex = 0; el.setAttribute('role', 'region');
      el.addEventListener('keydown', (e) => {
        const d = { ArrowDown: 40, ArrowUp: -40, PageDown: 300, PageUp: -300 }[e.key];
        if (d) { e.preventDefault(); this.scrollBy(it.key, d); }
      });
    } else {
      el = document.createElement(it.type === 'heading' ? 'h2' : 'p');
    }
    el.dataset.kind = it.type;
    el.dataset.key = it.key;
    el.addEventListener('focus', () => { this.focusKey = it.key; this.dirty = true; });
    el.addEventListener('blur', () => { if (this.focusKey === it.key) { this.focusKey = null; this.dirty = true; } });
    return el;
  }

  _update(el, it) {
    if (it.type === 'button') {
      if (el.textContent !== it.label) el.textContent = it.label;
      el.disabled = !!it.disabled;
      if (it.pressed != null) el.setAttribute('aria-pressed', it.pressed ? 'true' : 'false');
    } else if (it.type === 'field' || it.type === 'check') {
      if (el.getAttribute('aria-label') !== it.label) el.setAttribute('aria-label', it.label);
      el.disabled = !!it.disabled;
      if (it.type === 'field' && it.maxLength) el.maxLength = it.maxLength;
    } else if (it.type === 'scroll') {
      if (el.getAttribute('aria-label') !== it.label) el.setAttribute('aria-label', it.label);
      if (el.textContent !== it.text) el.textContent = it.text;
    } else if (el.textContent !== it.text) el.textContent = it.text;
  }

  _ctl(key) { return this.controls.find((c) => c.key === key); }

  // bẫy Tab trong bảng modal
  _trap(e) {
    if (e.key !== 'Tab' || !this.opts.modal) return;
    const f = [...this.sr.querySelectorAll('button:not([disabled]),textarea:not([disabled]),input:not([disabled]),[tabindex="0"]')];
    if (!f.length) return;
    const i = f.indexOf(document.activeElement);
    if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus({ preventScroll: true }); }
    else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus({ preventScroll: true }); }
  }

  focusFirst() {
    const el = this.sr.querySelector('button:not([disabled]),textarea:not([disabled]),input:not([disabled]),[tabindex="0"]');
    if (el) el.focus({ preventScroll: true });
  }
  focus(key) { const el = this.els.get(key); if (el) el.focus({ preventScroll: true }); }

  // Kích hoạt điều khiển (bấm chuột trên canvas HOẶC Enter/Space trên nút DOM) — luôn là hành động của người.
  activate(key) {
    const c = this._ctl(key);
    if (!c || c.disabled) return;
    if (c.type === 'field') { this.focus(key); return; }
    if (c.type === 'check') { const el = this.els.get(key); if (el && !el.disabled) { el.checked = !el.checked; this.dirty = true; } this.focus(key); return; }
    if (c.type === 'scroll') { this.focus(key); return; }
    if (c.onClick) c.onClick();
  }

  // ---------------------------------------------------------------- chuột (uv từ raycast)
  _px(uv) { return { x: uv.x * this.W, y: (1 - uv.y) * this.H }; }
  hit(uv) {
    const p = this._px(uv);
    for (let i = this.controls.length - 1; i >= 0; i--) {
      const c = this.controls[i];
      if (p.x >= c.x && p.x <= c.x + c.w && p.y >= c.y && p.y <= c.y + c.h) return { c, p };
    }
    return null;
  }
  click(uv) {
    const h = this.hit(uv);
    if (!h) return false;
    if (h.c.type === 'scroll' && h.c.arrows) {
      const a = h.c.arrows;
      if (h.p.x >= a.x && h.p.y <= a.y + a.s) { this.scrollBy(h.c.key, -h.c.h * 0.8); return true; }
      if (h.p.x >= a.x && h.p.y >= a.y2) { this.scrollBy(h.c.key, h.c.h * 0.8); return true; }
    }
    if (h.c.type === 'button') {
      const el = this.els.get(h.c.key);
      if (el && !el.disabled) el.focus({ preventScroll: true });
    }
    this.activate(h.c.key);
    return true;
  }
  hover(uv) {
    const h = uv ? this.hit(uv) : null;
    const k = h && h.c.type !== 'text' ? h.c.key : null;
    if (k !== this.hoverKey) { this.hoverKey = k; this.dirty = true; }
    return !!k;
  }
  wheel(uv, dy) {
    const h = this.hit(uv);
    const c = h && h.c.type === 'scroll' ? h.c : this.controls.find((x) => x.type === 'scroll');
    if (!c) return false;
    this.scrollBy(c.key, dy);
    return true;
  }
  scrollBy(key, dy) {
    const s = this.scroll[key] || { y: 0, max: 0 };
    s.y = Math.max(0, Math.min(s.max, s.y + dy));
    s.stick = s.y >= s.max - 2;
    this.scroll[key] = s;
    this.dirty = true;
  }

  dispose() {
    this.sr.remove();
    this.tex.dispose(); this.mesh.geometry.dispose(); this.mesh.material.dispose();
    this.mesh.removeFromParent();
  }
}

// ------------------------------------------------------------------ bộ vẽ + đăng ký điều khiển
class UI {
  constructor(panel, ctx) {
    this.p = panel; this.ctx = ctx; this.W = panel.W; this.H = panel.H;
    this.controls = []; this.items = [];
  }
  bg(radius) {
    const ctx = this.ctx;
    ctx.fillStyle = C.bg; roundRect(ctx, 4, 4, this.W - 8, this.H - 8, radius || 28); ctx.fill();
    ctx.strokeStyle = C.line; ctx.lineWidth = 3; ctx.stroke();
  }
  // chữ một dòng (đã cắt theo bề rộng); sr: chỉ khi truyền srKey
  text(x, y, s, font, color, maxW, srKey) {
    const ctx = this.ctx;
    ctx.font = font; ctx.fillStyle = color || C.text; ctx.textBaseline = 'middle';
    let t = clean(s);
    if (maxW && ctx.measureText(t).width > maxW) { while (t.length > 1 && ctx.measureText(t + '…').width > maxW) t = t.slice(0, -1); t += '…'; }
    ctx.fillText(t, x, y);
    if (srKey) this.items.push({ key: srKey, type: 'text', text: String(s == null ? '' : s) });
  }
  heading(key, x, y, s, size, color, maxW) {
    this.text(x, y, s, '700 ' + (size || 40) + 'px ' + FONT, color || C.text, maxW);
    this.items.push({ key, type: 'heading', text: String(s) });
  }
  // đoạn chữ xuống dòng; trả chiều cao đã dùng
  para(key, x, y, w, s, font, color, lineH, maxLines) {
    const ctx = this.ctx;
    ctx.font = font; ctx.fillStyle = color || C.text; ctx.textBaseline = 'top';
    const lines = wrapLines(ctx, clean(s), w, maxLines);
    lines.forEach((l, i) => ctx.fillText(l, x, y + i * lineH));
    if (key) this.items.push({ key, type: 'text', text: String(s == null ? '' : s) });
    return lines.length * lineH;
  }
  button(key, x, y, w, h, label, o) {
    const opt = o || {}, ctx = this.ctx, p = this.p;
    const disabled = !!opt.disabled || p.busy;
    const kind = opt.kind || 'ghost';
    const hover = p.hoverKey === key && !disabled, focus = p.focusKey === key;
    let bg = kind === 'primary' ? C.accent : kind === 'danger' ? C.danger : kind === 'ok' ? C.ok : C.ghost;
    let fg = kind === 'ghost' ? C.text : '#ffffff';
    if (opt.pressed) { bg = C.text; fg = '#ffffff'; }
    ctx.globalAlpha = disabled ? 0.42 : 1;
    ctx.fillStyle = bg; roundRect(ctx, x, y, w, h, Math.min(h / 2, 18)); ctx.fill();
    if (hover) { ctx.fillStyle = 'rgba(255,255,255,0.16)'; roundRect(ctx, x, y, w, h, Math.min(h / 2, 18)); ctx.fill(); }
    ctx.globalAlpha = 1;
    if (focus) { ctx.strokeStyle = C.focus; ctx.lineWidth = 5; roundRect(ctx, x - 5, y - 5, w + 10, h + 10, Math.min(h / 2, 18) + 5); ctx.stroke(); }
    ctx.globalAlpha = disabled ? 0.6 : 1;
    ctx.fillStyle = fg; ctx.font = (opt.font || '700 ' + Math.round(h * 0.4) + 'px ' + FONT); ctx.textBaseline = 'middle'; ctx.textAlign = 'center';
    let t = label;
    while (t.length > 1 && ctx.measureText(t).width > w - 20) t = t.slice(0, -2) + '…';
    ctx.fillText(t, x + w / 2, y + h / 2 + 1);
    ctx.textAlign = 'left'; ctx.globalAlpha = 1;
    this.controls.push({ key, type: 'button', x, y, w, h, disabled, onClick: opt.onClick });
    this.items.push({ key, type: 'button', label: opt.sr || label, disabled, pressed: opt.pressed });
  }
  // ô nhập: giá trị nằm ở <textarea> DOM; canvas vẽ lại theo sự kiện input (IME chạy bình thường)
  field(key, x, y, w, h, o) {
    const opt = o || {}, ctx = this.ctx, p = this.p;
    const focus = p.focusKey === key;
    this.items.push({ key, type: 'field', label: opt.label || '', rows: opt.rows || 1, disabled: p.busy || opt.disabled, maxLength: opt.maxLength });
    const el = p.els.get(key);
    const val = el ? el.value : '';
    ctx.fillStyle = '#ffffff'; roundRect(ctx, x, y, w, h, 14); ctx.fill();
    ctx.strokeStyle = focus ? C.focus : C.line; ctx.lineWidth = focus ? 4 : 3; ctx.stroke();
    const fs = opt.fontSize || 26, lh = Math.round(fs * 1.3);
    ctx.font = '500 ' + fs + 'px ' + FONT; ctx.textBaseline = 'top';
    ctx.save(); ctx.beginPath(); ctx.rect(x + 8, y + 6, w - 16, h - 12); ctx.clip();
    if (!val) { ctx.fillStyle = C.muted; ctx.fillText(opt.placeholder || '', x + 18, y + (h - lh) / 2 + 2); if (focus) { ctx.fillStyle = C.text; ctx.fillRect(x + 16, y + (h - lh) / 2 + 2, 3, lh - 4); } }
    else {
      const lines = wrapLines(ctx, val, w - 40);
      const vis = Math.max(1, Math.floor((h - 12) / lh));
      const from = Math.max(0, lines.length - vis);
      ctx.fillStyle = C.text;
      lines.slice(from).forEach((l, i) => ctx.fillText(l, x + 18, y + 10 + i * lh));
      if (focus) {
        const last = lines[lines.length - 1] || '', li = lines.length - 1 - from;
        ctx.fillRect(x + 18 + ctx.measureText(last).width + 2, y + 10 + li * lh, 3, lh - 4);
      }
    }
    ctx.restore();
    this.controls.push({ key, type: 'field', x, y, w, h, onKey: opt.onKey });
    return val;
  }
  check(key, x, y, w, label) {
    const ctx = this.ctx, p = this.p, el = p.els.get(key), on = !!(el && el.checked), focus = p.focusKey === key;
    ctx.fillStyle = on ? C.accent : '#ffffff'; roundRect(ctx, x, y, 40, 40, 8); ctx.fill();
    ctx.strokeStyle = focus ? C.focus : C.muted; ctx.lineWidth = focus ? 4 : 3; ctx.stroke();
    if (on) { ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 5; ctx.beginPath(); ctx.moveTo(x + 9, y + 21); ctx.lineTo(x + 17, y + 29); ctx.lineTo(x + 31, y + 11); ctx.stroke(); }
    const h = this.para(null, x + 56, y + 4, w - 60, label, '600 25px ' + FONT, C.text, 32, 3);
    this.controls.push({ key, type: 'check', x, y, w, h: Math.max(40, h) });
    this.items.push({ key, type: 'check', label, disabled: p.busy });
    return on;
  }
  // hộp cuộn: items = [{h, draw(ctx, x, y, w)}]; srText = toàn bộ chữ cho trình đọc màn hình
  scroll(key, x, y, w, h, items, srLabel, srText, o) {
    const opt = o || {}, ctx = this.ctx, p = this.p;
    const total = items.reduce((a, it) => a + it.h, 0);
    const s = p.scroll[key] || { y: 0, max: 0, stick: !!opt.stick };
    s.max = Math.max(0, total - (h - 16));
    if (s.stick || opt.forceBottom) s.y = s.max;
    s.y = Math.min(s.y, s.max);
    p.scroll[key] = s;
    ctx.fillStyle = opt.bg || C.code; roundRect(ctx, x, y, w, h, 14); ctx.fill();
    if (p.focusKey === key) { ctx.strokeStyle = C.focus; ctx.lineWidth = 4; roundRect(ctx, x, y, w, h, 14); ctx.stroke(); }
    ctx.save(); ctx.beginPath(); ctx.rect(x + 2, y + 2, w - 4, h - 4); ctx.clip();
    let cy = y + 8 - s.y;
    const iw = w - (s.max > 0 ? 70 : 24);
    for (const it of items) {
      if (cy + it.h >= y && cy <= y + h) it.draw(ctx, x + 14, cy, iw);
      cy += it.h;
    }
    ctx.restore();
    let arrows = null;
    if (s.max > 0) {
      // thanh cuộn + nút ▲▼ (chạm được trên điện thoại)
      const ax = x + w - 50, as = 44;
      ctx.fillStyle = 'rgba(61,57,41,0.12)'; roundRect(ctx, ax + 16, y + as + 8, 12, h - 2 * as - 16, 6); ctx.fill();
      const th = Math.max(30, (h - 2 * as - 16) * (h / (total + 16)));
      const ty = y + as + 8 + (h - 2 * as - 16 - th) * (s.y / s.max);
      ctx.fillStyle = 'rgba(61,57,41,0.45)'; roundRect(ctx, ax + 16, ty, 12, th, 6); ctx.fill();
      ctx.fillStyle = C.muted; ctx.font = '700 26px ' + FONT; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText('▲', ax + 22, y + as / 2 + 4); ctx.fillText('▼', ax + 22, y + h - as / 2 - 4); ctx.textAlign = 'left';
      arrows = { x: ax, y, s: as, y2: y + h - as };
    }
    this.controls.push({ key, type: 'scroll', x, y, w, h, arrows });
    this.items.push({ key, type: 'scroll', label: srLabel, text: srText });
  }
}

// Dòng chữ (đã chặt) thành item cho hộp cuộn.
export function lineItems(ctx, text, w, font, color, lineH) {
  ctx.font = font;
  return wrapLines(ctx, clean(text), w).map((l) => ({ h: lineH, draw: (c, x, y) => { c.font = font; c.fillStyle = color; c.textBaseline = 'top'; c.fillText(l, x, y + 2); } }));
}
