/* Văn phòng 3D — giai đoạn 2: duyệt thao tác, trả lời câu hỏi, đăng xuất NGAY TRONG CẢNH (hợp đồng: docs/dev-notes/office-3d.md, "Giai đoạn 2").
 * Mọi thao tác ghi đi qua CÙNG cổng với web 2D: POST /office/do/{name} (post_guard: CSRF + khoá phiên `k` + cùng nguồn) → JSON từng bước
 * need:"confirm" → need:"confirm2" → ok. `confirm=yes` / `confirm2=yes` CHỈ được gửi từ trình xử lý nút do NGƯỜI bấm (chuột hoặc Enter/Space)
 * trên đúng bước đó; khi đang gửi, mọi nút bị khoá (không gửi đôi). Bảng là mặt phẳng gắn vào camera ("billboard lớn trước camera").
 * Chữ của agent (lệnh, câu hỏi) chỉ vẽ bằng fillText. Khoá phiên/CSRF không bao giờ được vẽ, in hay ghi log. */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { FONT } from './util.js';
import { Panel, COLORS as C, MONO, lineItems, clean } from './ui.js';
import { fmtDur } from './boards.js';

const GRANT_LABEL = { '': 'Không hạn', '8h': '8 giờ', '24h': '24 giờ', '7d': '7 ngày', '30d': '30 ngày' };
const STATUS_AP = { open: 'đang chờ', waiting_human: 'chờ bạn', allowed: 'đã cho phép', denied: 'đã từ chối', cancelled: 'đã huỷ' };

// ------------------------------------------------------------------ gọi server (CSRF + khoá phiên)
export function makeApi(root) {
  const csrf = () => root.getAttribute('data-csrf') || '';
  const key = () => { try { return localStorage.getItem('ahub_k') || ''; } catch (_) { return ''; } };
  async function getJSON(url) {
    const r = await fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
    if (r.status === 401) return { status: 401, error: 'Phiên đăng nhập đã hết' };
    let d = null;
    try { d = await r.json(); } catch (_) { /* không phải JSON */ }
    if (!r.ok) return { status: r.status, error: (d && (d.error || d.detail)) || ('Lỗi ' + r.status) };
    return { status: r.status, data: d };
  }
  // fields: object; mảng → lặp khoá (keys=…)
  async function post(url, fields) {
    const body = new URLSearchParams();
    body.append('csrf', csrf());
    body.append('k', key());
    for (const [k, v] of Object.entries(fields || {})) {
      if (v == null) continue;
      if (Array.isArray(v)) v.forEach((x) => body.append(k, String(x))); else body.append(k, String(v));
    }
    let r;
    try {
      r = await fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'X-Ahub-Fetch': '1', Accept: 'application/json' }, body });
    } catch (_) { return { status: 0, error: 'Không kết nối được tới hub' }; }
    if (r.status === 401) return { status: 401, error: 'Phiên đăng nhập đã hết' };
    let d = null;
    try { d = await r.json(); } catch (_) { /* thân không phải JSON */ }
    if (!r.ok || (d && d.ok === false)) return { status: r.status, error: (d && (d.error || d.detail)) || (r.status === 403 ? 'Bị từ chối (CSRF/khoá phiên) — tải lại trang' : 'Lỗi ' + r.status), data: d };
    return { status: r.status, data: d || {} };
  }
  async function logout() {
    const body = new URLSearchParams();
    body.append('csrf', csrf());
    try { await fetch('/logout', { method: 'POST', credentials: 'same-origin', body, redirect: 'manual' }); } catch (_) { /* vẫn xoá khoá phía trình duyệt */ }
    try { localStorage.removeItem('ahub_k'); localStorage.removeItem('cfh_view'); } catch (_) { /* bỏ qua */ }
    location.href = '/login';
  }
  return { getJSON, post, logout };
}

// ------------------------------------------------------------------ chồng bảng modal gắn camera
export class Modals {
  constructor(o) {
    this.camera = o.camera; this.srRoot = o.srRoot; this.onChange = o.onChange || (() => {});
    this.stack = [];
    this.backdrop = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ color: '#000000', transparent: true, opacity: 0.38, depthTest: false, depthWrite: false }));
    this.backdrop.renderOrder = 1000; this.backdrop.visible = false;
    this.camera.add(this.backdrop);
  }
  get top() { return this.stack[this.stack.length - 1] || null; }
  get open() { return this.stack.length > 0; }
  portrait() { return this.camera.aspect < 0.85; }
  push(opts) {
    const portrait = this.portrait();
    const p = new Panel(Object.assign({ srRoot: this.srRoot, modal: true }, opts, {
      pxW: portrait ? opts.pxP[0] : opts.pxL[0], pxH: portrait ? opts.pxP[1] : opts.pxL[1],
    }));
    for (const q of this.stack) { q.mesh.visible = false; q.sr.hidden = true; }
    this.stack.push(p);
    this.camera.add(p.mesh);
    this.fit();
    p.draw();
    setTimeout(() => p.focusFirst(), 0);
    this.onChange();
    return p;
  }
  pop(p) {
    const i = this.stack.indexOf(p || this.top);
    if (i < 0) return;
    const [q] = this.stack.splice(i, 1);
    q.dispose();
    const t = this.top;
    if (t) { t.mesh.visible = true; t.sr.hidden = false; t.dirty = true; setTimeout(() => t.focusFirst(), 0); }
    this.fit();
    this.onChange();
  }
  clear() { while (this.stack.length) this.pop(); }
  // đặt bảng trước camera, vừa khung nhìn
  fit() {
    const d = 1.6, cam = this.camera;
    const vh = 2 * d * Math.tan((cam.fov * Math.PI) / 360), vw = vh * cam.aspect;
    this.backdrop.visible = this.stack.length > 0;
    this.backdrop.position.set(0, 0, -d - 0.05); this.backdrop.scale.set(vw * 1.3, vh * 1.3, 1);
    for (const p of this.stack) {
      const s = Math.min((0.95 * vw) / p.W, (0.9 * vh) / p.H);
      p.mesh.scale.set(p.W * s, p.H * s, 1);
      p.mesh.position.set(0, 0, -d);
    }
  }
  update() { for (const p of this.stack) p.draw(); }
}

// ------------------------------------------------------------------ các luồng
export function createFlows(o) {
  // o: {api, modals, names(id), toast(msg, ok), refresh(), pop(agentId, emoji), onExpired()}
  const { api, modals, names } = o;

  function errorOf(res) {
    if (res.status === 401) { modals.clear(); o.onExpired(); return 'Phiên đăng nhập đã hết'; }
    return res.error || 'Lỗi';
  }

  // Chạy một thao tác theo giao thức bước. `fields` KHÔNG chứa confirm/confirm2 ở lần đầu.
  async function run(name, fields, ctx) {
    const owner = ctx.panel;
    owner.busy = true; owner.dirty = true;
    const res = await api.post('/office/do/' + encodeURIComponent(name), fields);
    owner.busy = false; owner.dirty = true;
    if (res.error) { o.toast(errorOf(res), false); return; }
    const d = res.data || {};
    // bảng xác nhận/bước 2 hiện tại được THAY bằng bước kế (Esc ở bước sau quay về bảng gốc, không về bước cũ)
    if ((d.need === 'confirm' || d.need === 'confirm2') && ctx.replace) modals.pop(owner);
    if (d.need === 'confirm') { confirmStep(name, fields, d, ctx); return; }
    if (d.need === 'confirm2') { step2(name, fields, d, ctx); return; }
    if (d.ok) {
      o.toast(d.message || 'Đã xong', true);
      if (ctx.agent) o.pop(ctx.agent, name === 'deny' ? '❌' : '✅');
      modals.clear();
      o.refresh();
      return;
    }
    o.toast('Phản hồi không rõ từ hub', false);
  }

  // Bước xác nhận (thông điệp do server soạn; ô ghi chú nếu có note_label)
  function confirmStep(name, fields, d, ctx) {
    const nl = String(d.message || '').split('\n').reduce((a, l) => a + Math.max(1, Math.ceil(l.length / 60)), 0);
    const p = modals.push({
      title: 'Xác nhận', pxL: [1100, Math.min(720, 300 + Math.min(nl, 9) * 36 + (d.note_label ? 190 : 0))], pxP: [760, Math.min(1100, 360 + Math.min(nl * 1.6, 14) * 36 + (d.note_label ? 190 : 0))],
      onEscape: () => modals.pop(p),
      build: (ui) => {
        const W = ui.W, H = ui.H, pad = 44;
        ui.bg();
        ui.heading('h', pad, 60, 'Xác nhận', 40, d.danger ? C.danger : C.text, W - 2 * pad);
        let y = 100;
        y += ui.para('m', pad, y, W - 2 * pad, d.message || '', '500 27px ' + FONT, C.text, 36, d.note_label ? 9 : 14) + 16;
        if (d.note_label) {
          ui.text(pad, y + 14, d.note_label, '600 24px ' + FONT, C.muted, W - 2 * pad);
          ui.field('note', pad, y + 34, W - 2 * pad, 120, { label: d.note_label, rows: 3, placeholder: 'Viết lý do để agent biết làm khác…', maxLength: 2000 });
        }
        const bw = Math.min(300, (W - 2 * pad - 24) / 2), by = H - 110;
        ui.button('cancel', W - pad - 2 * bw - 24, by, bw, 72, 'Huỷ', { onClick: () => modals.pop(p) });
        ui.button('yes', W - pad - bw, by, bw, 72, d.danger ? 'Đồng ý từ chối' : 'Đồng ý', {
          kind: d.danger ? 'danger' : 'primary',
          // NGƯỜI bấm → mới gửi confirm=yes
          onClick: () => {
            const extra = { confirm: 'yes' };
            if (d.note_label) { const n = p.value('note').trim(); if (n) extra.note = n; }
            run(name, Object.assign({}, fields, extra), Object.assign({}, ctx, { panel: p, replace: true }));
          },
        });
      },
    });
  }

  // Bước 2 (thao tác nặng / luôn cho phép / luôn cho phép theo luật): nêu rõ luật, đọc hết lệnh, tích ô, (chọn thời hạn) rồi mới gửi confirm2=yes
  function step2(name, fields, d, ctx) {
    const view = d.view || {};
    const ap = ctx.detail || {};
    const agent = names(ap.agent) || ap.agent || '?';
    let grant = null; // thời hạn người chọn (bắt buộc chọn rõ)
    const kind = d.kind || 'heavy';
    const title = kind === 'grant' ? 'Xác nhận lần 2 — luôn cho phép theo luật' : kind === 'always' ? 'Xác nhận lần 2 — luôn cho phép' : 'Xác nhận lần 2 — thao tác phá huỷ nặng';
    const rule = d.rule || ap.matched_rule || ap.rule || null;
    const stmt = kind === 'grant'
      ? 'Cho phép thao tác dưới đây lần này VÀ từ nay KHÔNG hỏi lại agent ' + agent + ' với luật ' + (rule || ('đã khớp của yêu cầu này' + (ap.klass ? ' (lớp ' + ap.klass + ')' : ''))) +
        ' — kể cả thao tác phá huỷ khác cùng luật, ở mọi task. Các yêu cầu đang chờ cùng agent + luật cũng được cho phép luôn.'
      : kind === 'always'
        ? 'Cho phép thao tác dưới đây của agent ' + agent + ' lần này VÀ tạo ' + ((d.always && d.always.summary) || 'luật luôn cho phép đúng thao tác này') +
          '. Lần sau gặp đúng thao tác đó hub tự cho phép, không hỏi bạn.'
        : 'Agent ' + agent + ' muốn chạy thao tác dưới đây' + (ap.klass ? ' (lớp ' + ap.klass + ')' : '') + '. Đọc TOÀN BỘ lệnh trước khi cho phép — thao tác có thể gây mất dữ liệu.';
    const checkLabel = kind === 'grant' ? 'Tôi muốn hub không hỏi lại ' + agent + ' với luật này'
      : kind === 'always' ? 'Tôi đã đọc toàn bộ lệnh và muốn hub LUÔN cho phép đúng thao tác này'
        : 'Tôi đã đọc toàn bộ lệnh và cho phép chạy đúng MỘT lần';
    const p = modals.push({
      title, ...sizeFor(view.text, [520 + (kind === 'grant' ? 120 : 0) + (view.hidden_chars ? 56 : 0), 760 + (kind === 'grant' ? 120 : 0)], 860, 1240, 80, 46),
      onEscape: () => modals.pop(p),
      build: (ui) => {
        const W = ui.W, H = ui.H, pad = 40, ctx2 = ui.ctx;
        ui.bg();
        ui.heading('h', pad, 52, title, 36, C.danger, W - 2 * pad);
        let y = 86;
        y += ui.para('stmt', pad, y, W - 2 * pad, stmt, '600 25px ' + FONT, C.text, 33, 6) + 10;
        if (view.hidden_chars) {
          ctx2.fillStyle = C.warnBg; ctx2.fillRect(pad, y, W - 2 * pad, 46);
          ui.text(pad + 14, y + 23, '⚠ Lệnh chứa ký tự ẩn/điều khiển (hiện là ·) — đọc kỹ từng dòng', '700 23px ' + FONT, C.danger, W - 2 * pad - 28, 'hid');
          y += 56;
        }
        const grantH = kind === 'grant' ? 120 : 0;
        const boxH = H - y - 200 - grantH;
        const txt = view.text || '';
        ui.scroll('cmd', pad, y, W - 2 * pad, Math.max(120, boxH), lineItems(ctx2, txt, W - 2 * pad - 70, '500 23px ' + MONO, C.text, 30),
          'Toàn bộ thao tác (' + (view.tool || '') + ')', txt);
        y += Math.max(120, boxH) + 14;
        if (kind === 'grant') {
          ui.text(pad, y + 16, 'Thời hạn (chọn một):', '600 23px ' + FONT, C.muted, 260);
          const opts = (d.grant_for && d.grant_for.length ? d.grant_for : ['', '8h', '24h', '7d', '30d']).filter((k) => k in GRANT_LABEL);
          const bw = Math.min(170, (W - 2 * pad) / opts.length - 10);
          opts.forEach((k, i) => ui.button('g' + i, pad + i * (bw + 10), y + 40, bw, 56, GRANT_LABEL[k], {
            pressed: grant === k, sr: 'Thời hạn: ' + GRANT_LABEL[k], onClick: () => { grant = k; p.dirty = true; },
          }));
          y += grantH;
        }
        const ok = ui.check('ack', pad, y, W - 2 * pad, checkLabel);
        const bw = Math.min(330, (W - 2 * pad - 24) / 2), by = H - 100;
        ui.button('cancel', W - pad - 2 * bw - 24, by, bw, 70, 'Huỷ', { onClick: () => modals.pop(p) });
        ui.button('yes', W - pad - bw, by, bw, 70, kind === 'heavy' ? 'Cho phép (bước 2)' : 'Luôn cho phép (bước 2)', {
          kind: 'danger', disabled: !ok || (kind === 'grant' && grant == null),
          onClick: () => {
            if (!p.checked('ack') || (kind === 'grant' && grant == null)) return;
            const extra = { confirm: 'yes', confirm2: 'yes' };
            if (kind === 'grant') extra.grant_for = grant;
            run(name, Object.assign({}, fields, extra), Object.assign({}, ctx, { panel: p, replace: true }));
          },
        });
      },
    });
  }

  // Chiều cao canvas theo độ dài thao tác (lệnh ngắn → bảng gọn; lệnh dài → cao tối đa, cuộn)
  function sizeFor(text, base, maxL, maxP, perLineL, perLineP) {
    const lines = String(text || '').split('\n').reduce((a, l) => a + Math.max(1, Math.ceil(l.length / perLineL)), 0);
    const linesP = String(text || '').split('\n').reduce((a, l) => a + Math.max(1, Math.ceil(l.length / perLineP)), 0);
    return { pxL: [1280, Math.min(maxL, base[0] + lines * 30)], pxP: [780, Math.min(maxP, base[1] + linesP * 30)] };
  }

  // Bảng duyệt: toàn bộ thao tác trước nút "Cho phép"
  async function openApproval(id) {
    const res = await api.getJSON('/office/approval/' + encodeURIComponent(id));
    if (res.error) { o.toast(errorOf(res), false); return; }
    const ap = res.data || {};
    const view = ap.view || {};
    const agent = names(ap.agent) || ap.agent || '?';
    const done = ap.status && !['open', 'waiting_human'].includes(ap.status);
    const base = { id: ap.id, preview: clean(view.text || '').replace(/\s+/g, ' ').trim().slice(0, 160) };
    const ctx = { agent: ap.agent, detail: ap };
    const p = modals.push({
      title: 'Phê duyệt — ' + agent, ...sizeFor(view.text, [360 + 82 * Math.ceil((2 + !!ap.always_offer + !!ap.can_rule) / 2) + (view.hidden_chars ? 58 : 0) + (ap.heavy ? 36 : 0),
        460 + 82 * (2 + !!ap.always_offer + !!ap.can_rule) + (view.hidden_chars ? 58 : 0)], 860, 1280, 80, 46),
      onEscape: () => modals.pop(p),
      build: (ui) => {
        const W = ui.W, H = ui.H, pad = 40, ctx2 = ui.ctx, portrait = W < H;
        ui.bg();
        ui.heading('h', pad, 50, 'Phê duyệt · ' + agent, 38, C.text, W - 2 * pad - 70);
        ui.button('x', W - pad - 56, 22, 56, 56, '✕', { sr: 'Đóng', onClick: () => modals.pop(p) });
        const meta = [(ap.tool || '?') + (ap.klass ? ' [' + ap.klass + ']' : ''), 'chờ ' + fmtDur(ap.wait_s || 0), STATUS_AP[ap.status] || ap.status || '',
          ap.routed_to === 'manager' ? 'đã giao điều phối viên' : ''].filter(Boolean).join(' · ');
        ui.text(pad, 98, meta, '600 24px ' + FONT, C.muted, W - 2 * pad, 'meta');
        let y = 124;
        if (view.hidden_chars) {
          ctx2.fillStyle = C.warnBg; ctx2.fillRect(pad, y, W - 2 * pad, 48);
          ui.text(pad + 14, y + 24, '⚠ Thao tác chứa ký tự ẩn/điều khiển (hiện là ·) — đọc kỹ trước khi cho phép', '700 23px ' + FONT, C.danger, W - 2 * pad - 28, 'hid');
          y += 58;
        }
        if (ap.heavy) { ui.text(pad, y + 16, 'Thao tác nặng — cần xác nhận 2 bước.', '600 22px ' + FONT, C.danger, W - 2 * pad, 'heavy'); y += 36; }
        const btns = [
          ['approve', 'Cho phép một lần', 'primary', true],
          ['deny', 'Từ chối…', 'danger', true],
          ['approve_always', 'Luôn cho phép…', 'ghost', !!ap.always_offer],
          ['approve_rule', 'Luôn cho phép theo luật…', 'ghost', !!ap.can_rule],
        ].filter((b) => b[3]);
        const cols = portrait ? 1 : 2;
        const rows = Math.ceil(btns.length / cols);
        const btnArea = rows * 82 + 30;
        const boxH = H - y - btnArea - 10;
        const txt = view.text || '';
        ui.scroll('cmd', pad, y, W - 2 * pad, boxH, lineItems(ctx2, txt, W - 2 * pad - 70, '500 23px ' + MONO, C.text, 30),
          'Toàn bộ thao tác (' + (view.tool || ap.tool || '') + ')', txt);
        const bw = (W - 2 * pad - (cols - 1) * 16) / cols;
        btns.forEach(([n, label, kind], i) => {
          const bx = pad + (i % cols) * (bw + 16), by = H - btnArea + 10 + Math.floor(i / cols) * 82;
          ui.button(n, bx, by, bw, 68, label, { kind, disabled: done, onClick: () => run(n, base, Object.assign({}, ctx, { panel: p })) });
        });
        if (done) ui.text(pad, H - 24, 'Yêu cầu này đã được xử lý.', '600 22px ' + FONT, C.muted, W - 2 * pad, 'done');
      },
    });
  }

  // Bảng câu hỏi: chữ đầy đủ + phương án + ô trả lời tự do
  async function openQuestion(id) {
    const res = await api.getJSON('/office/question/' + encodeURIComponent(id));
    if (res.error) { o.toast(errorOf(res), false); return; }
    const q = res.data || {};
    const agent = names(q.agent) || q.agent || '?';
    const picked = new Set();
    const p = modals.push({
      title: 'Câu hỏi — ' + agent, pxL: [1200, 840], pxP: [780, 1240],
      onEscape: () => modals.pop(p),
      build: (ui) => {
        const W = ui.W, H = ui.H, pad = 40, ctx2 = ui.ctx;
        ui.bg();
        ui.heading('h', pad, 50, '❓ ' + agent + ' hỏi bạn' + (q.multi ? ' (chọn được nhiều)' : ''), 38, C.text, W - 2 * pad - 70);
        ui.button('x', W - pad - 56, 22, 56, 56, '✕', { sr: 'Đóng', onClick: () => modals.pop(p) });
        const opts = q.options || [];
        const optRows = Math.ceil(opts.length / (W < H ? 1 : 2));
        const freeH = q.allow_text !== false ? 150 : 0;
        const boxH = Math.max(140, H - 100 - optRows * 76 - freeH - 140);
        const txt = q.text || '';
        ui.scroll('qt', pad, 90, W - 2 * pad, boxH, lineItems(ctx2, txt, W - 2 * pad - 70, '500 27px ' + FONT, C.text, 36), 'Câu hỏi', txt, { bg: '#f2eef8' });
        let y = 90 + boxH + 16;
        const cols = W < H ? 1 : 2, bw = (W - 2 * pad - (cols - 1) * 14) / cols;
        opts.forEach((op, i) => {
          ui.button('o' + i, pad + (i % cols) * (bw + 14), y + Math.floor(i / cols) * 76, bw, 64, (picked.has(op.key) ? '✓ ' : '') + clean(op.label), {
            pressed: picked.has(op.key), font: '600 24px ' + FONT, sr: 'Phương án: ' + op.label,
            onClick: () => { if (picked.has(op.key)) picked.delete(op.key); else { if (!q.multi) picked.clear(); picked.add(op.key); } p.dirty = true; },
          });
        });
        y += optRows * 76;
        if (q.allow_text !== false) {
          ui.text(pad, y + 14, 'Hoặc trả lời bằng chữ:', '600 23px ' + FONT, C.muted, W - 2 * pad);
          ui.field('free', pad, y + 34, W - 2 * pad, 100, { label: 'Câu trả lời', rows: 3, placeholder: 'Nhập câu trả lời…', maxLength: 4000 });
        }
        const text = ui.p.value('free').trim();
        const bw2 = Math.min(320, (W - 2 * pad - 20) / 2), by = H - 100;
        ui.button('cancel', W - pad - 2 * bw2 - 20, by, bw2, 70, 'Đóng', { onClick: () => modals.pop(p) });
        ui.button('send', W - pad - bw2, by, bw2, 70, 'Gửi trả lời', {
          kind: 'primary', disabled: !picked.size && !text,
          onClick: () => {
            const t = p.value('free').trim();
            if (!picked.size && !t) return;
            run('answer', { id: q.id, keys: [...picked], text: t || null }, { panel: p, agent: q.agent });
          },
        });
      },
    });
  }

  function openLogout() {
    const p = modals.push({
      title: 'Thoát', pxL: [980, 520], pxP: [760, 760],
      onEscape: () => modals.pop(p),
      build: (ui) => {
        const W = ui.W, H = ui.H, pad = 44;
        ui.bg();
        ui.heading('h', pad, 64, '🚪 Thoát khỏi cfharness?', 42, C.text, W - 2 * pad);
        ui.para('m', pad, 110, W - 2 * pad, 'Bạn sẽ đăng xuất khỏi giao diện web trên trình duyệt này. Các agent vẫn tiếp tục làm việc. Đăng nhập lại bằng: cfharness web --open',
          '500 27px ' + FONT, C.text, 36, 6);
        const bw = Math.min(300, (W - 2 * pad - 24) / 2), by = H - 110;
        ui.button('stay', W - pad - 2 * bw - 24, by, bw, 72, 'Ở lại', { onClick: () => modals.pop(p) });
        ui.button('out', W - pad - bw, by, bw, 72, 'Đăng xuất', { kind: 'danger', onClick: () => { p.busy = true; p.dirty = true; api.logout(); } });
      },
    });
  }

  return { openApproval, openQuestion, openLogout };
}
