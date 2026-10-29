/* Văn phòng 3D — mọi thông tin vẽ TRONG cảnh (chế độ 3D = toàn trang 3D): màn hình HUD trên tường, bảng kanban, màn hình phòng
 * server (run đang chạy), bảng "Cần bạn" ở lối vào, thẻ thông tin nhân vật, thẻ đỏ "cần duyệt" trên đầu, biển "hết phiên/mất kết nối".
 * Chữ của agent (tên, task, preview…) là DỮ LIỆU: chỉ vẽ bằng fillText (đã cắt ở server, cắt thêm theo bề rộng ở đây). */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { FONT, fitText, roundRect } from './util.js';

const STATUS_COLOR = {
  working: '#4d7c43', waiting: '#a86414', waiting_human: '#c4483a', asking: '#7b5ea7', idle: '#73726c',
  error: '#c4483a', paused: '#73726c', queued: '#3f6d8c',
};

function tex(c) {
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 4;
  return t;
}

// Xuống dòng theo bề rộng (tối đa `max` dòng, dòng cuối có "…").
export function wrap(ctx, text, maxW, max) {
  const words = String(text || '').split(/\s+/).filter(Boolean);
  const lines = [];
  let cur = '';
  for (let i = 0; i < words.length; i++) {
    const t = cur ? cur + ' ' + words[i] : words[i];
    if (ctx.measureText(t).width <= maxW) { cur = t; continue; }
    if (cur) lines.push(cur);
    cur = words[i];
    if (lines.length === max - 1) { cur = words.slice(i).join(' '); break; }
  }
  if (cur) lines.push(cur);
  return lines.slice(0, max).map((l, i) => (i === max - 1 ? fitText(ctx, l, maxW) : fitText(ctx, l, maxW)));
}

export function fmtDur(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h ? h + 'h' + String(m).padStart(2, '0') : m + ':' + String(ss).padStart(2, '0');
}
export function fmtUsd(v) { const n = Number(v) || 0; return '$' + (n >= 100 ? n.toFixed(0) : n.toFixed(2)); }

// ------------------------------------------------------------ tấm phẳng có chữ (MeshBasic: không bị ánh sáng/ESTOP đổi màu)
export class Board {
  constructor(w, h, px, kind, label) {
    this.w = w; this.h = h;
    this.canvas = document.createElement('canvas');
    this.canvas.width = px; this.canvas.height = Math.round((px * h) / w);
    this.tex = tex(this.canvas);
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: this.tex, toneMapped: false }));
    this.mesh.userData.focus = { kind, label, board: this };
    this.sig = null;
  }
  draw(sig, fn) {
    if (sig === this.sig) return;
    this.sig = sig;
    fn(this.canvas.getContext('2d'), this.canvas.width, this.canvas.height);
    this.tex.needsUpdate = true;
  }
  // tâm + pháp tuyến theo thế giới (để camera bay tới nhìn thẳng)
  frame() {
    const c = new THREE.Vector3(), n = new THREE.Vector3(0, 0, 1);
    this.mesh.getWorldPosition(c);
    n.applyQuaternion(this.mesh.getWorldQuaternion(new THREE.Quaternion())).normalize();
    return { center: c, normal: n, w: this.w, h: this.h };
  }
  dispose() { this.tex.dispose(); this.mesh.geometry.dispose(); this.mesh.material.dispose(); }
}

function header(ctx, W, title, sub, bg, fg) {
  ctx.fillStyle = bg; ctx.fillRect(0, 0, W, 86);
  ctx.fillStyle = fg; ctx.font = '700 44px ' + FONT; ctx.textBaseline = 'middle';
  ctx.fillText(title, 32, 45);
  if (sub) { ctx.font = '600 30px ' + FONT; ctx.textAlign = 'right'; ctx.fillText(sub, W - 32, 46); ctx.textAlign = 'left'; }
}

// ------------------------------------------------------------ màn hình HUD trên tường bắc
export function drawHud(board, d) {
  const h = d.hub || {}, tc = d.task_counts || {};
  const tiles = [
    ['Đang chạy', String(h.running || 0), '#6cc6ff'],
    ['Xếp hàng', String(h.queued || 0), '#c9d3dc'],
    ['Chờ bạn duyệt', String(h.approvals_waiting_human || 0), h.approvals_waiting_human ? '#ff7b6e' : '#c9d3dc'],
    ['Chi phí hôm nay', fmtUsd(h.cost_today_usd), '#f2c76e'],
    ['ESTOP', h.estop ? 'BẬT' : 'Tắt', h.estop ? '#ff5a4a' : '#8fd18a'],
    ['Giới hạn tốc độ', h.rate_limited ? 'Đang chặn' : 'Không', h.rate_limited ? '#ffb454' : '#8fd18a'],
  ];
  const conn = d.conn === 'live' ? ['● Trực tiếp', '#8fd18a'] : d.conn === 'expired' ? ['● Phiên đã hết', '#ff7b6e'] : d.conn === 'lost' ? ['● Mất kết nối', '#ffb454'] : ['● Đang kết nối', '#c9d3dc'];
  const sig = JSON.stringify([tiles, conn, tc]);
  board.draw(sig, (ctx, W, H) => {
    ctx.fillStyle = '#1d2230'; ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = '#e9ecf2'; ctx.font = '700 46px ' + FONT; ctx.textBaseline = 'middle';
    ctx.fillText('cfharness · Văn phòng', 36, 50);
    ctx.font = '600 32px ' + FONT; ctx.textAlign = 'right'; ctx.fillStyle = conn[1]; ctx.fillText(conn[0], W - 36, 52); ctx.textAlign = 'left';
    const tw = (W - 72 - 5 * 18) / 6;
    tiles.forEach(([k, v, col], i) => {
      const x = 36 + i * (tw + 18), y = 100;
      ctx.fillStyle = '#2a3142'; roundRect(ctx, x, y, tw, 196, 22); ctx.fill();
      ctx.fillStyle = '#9aa3b5'; ctx.font = '600 28px ' + FONT; ctx.fillText(fitText(ctx, k, tw - 32), x + 18, y + 38);
      ctx.fillStyle = col; let fs = 72; ctx.font = '800 72px ' + FONT;
      while (fs > 34 && ctx.measureText(v).width > tw - 36) { fs -= 4; ctx.font = '800 ' + fs + 'px ' + FONT; }
      ctx.fillText(fitText(ctx, v, tw - 32), x + 18, y + 122);
    });
    ctx.fillStyle = '#9aa3b5'; ctx.font = '600 30px ' + FONT;
    ctx.fillText('Task: ' + (tc.ready || 0) + ' sẵn sàng · ' + (tc.running || 0) + ' đang làm · ' + (tc.review || 0) + ' review · ' + (tc.blocked || 0) + ' kẹt', 36, H - 38);
  });
}

// ------------------------------------------------------------ bảng kanban (tối đa 5 task mỗi cột)
const KCOLS = [['ready', 'Sẵn sàng', '#e9b44c'], ['running', 'Đang làm', '#4f8fc0'], ['review', 'Review', '#9c89b8'], ['blocked', 'Kẹt', '#e07a5f']];
export function drawKanban(board, counts, tasks, names) {
  const sig = JSON.stringify([counts, tasks]);
  board.draw(sig, (ctx, W, H) => {
    ctx.fillStyle = '#f7f0e1'; ctx.fillRect(0, 0, W, H);
    header(ctx, W, 'Bảng công việc', null, '#efe4cc', '#3d3929');
    const cw = (W - 40) / 4;
    KCOLS.forEach(([k, label, color], i) => {
      const x = 20 + i * cw, n = Math.max(0, Number((counts || {})[k]) || 0), list = ((tasks || {})[k]) || [];
      ctx.fillStyle = 'rgba(61,57,41,0.07)'; roundRect(ctx, x + 6, 104, cw - 12, H - 124, 18); ctx.fill();
      ctx.fillStyle = color; roundRect(ctx, x + 6, 104, cw - 12, 58, 18); ctx.fill();
      ctx.fillStyle = '#ffffff'; ctx.font = '700 30px ' + FONT; ctx.textBaseline = 'middle';
      ctx.fillText(fitText(ctx, label, cw - 100), x + 24, 134);
      ctx.textAlign = 'right'; ctx.fillText(String(n), x + cw - 26, 134); ctx.textAlign = 'left';
      list.slice(0, 5).forEach((t, j) => {
        const y = 172 + j * 92;
        ctx.fillStyle = '#fffdf8'; roundRect(ctx, x + 18, y, cw - 36, 86, 12); ctx.fill();
        ctx.fillStyle = color; ctx.fillRect(x + 18, y + 10, 6, 66);
        ctx.fillStyle = '#2d2a24'; ctx.font = '600 21px ' + FONT;
        wrap(ctx, t.title, cw - 66, 2).forEach((l, li) => ctx.fillText(l, x + 34, y + 20 + li * 24));
        if (t.assignee) { ctx.fillStyle = '#73726c'; ctx.font = '500 17px ' + FONT; ctx.fillText(fitText(ctx, '→ ' + (names(t.assignee) || t.assignee), cw - 66), x + 34, y + 70); }
      });
      if (n > list.length && list.length && list.length < 5) { ctx.fillStyle = '#73726c'; ctx.font = '600 22px ' + FONT; ctx.fillText('+' + (n - list.length) + ' việc nữa', x + 24, 172 + list.length * 92 + 20); }
      if (!n && !list.length) { ctx.fillStyle = '#a3a196'; ctx.font = '500 22px ' + FONT; ctx.fillText('(trống)', x + 24, 200); }
    });
  });
}

// ------------------------------------------------------------ màn hình phòng server: run đang chạy
export function drawRuns(board, runs, names, nowMs) {
  const rows = (runs || []).slice(0, 7).map((r) => {
    const t = Date.parse(r.started_at || '');
    return [names(r.agent) || r.agent || '—', isNaN(t) ? '—' : fmtDur((nowMs - t) / 1000), fmtUsd(r.cost_usd), r.id || ''];
  });
  board.draw(JSON.stringify(rows) + (runs || []).length, (ctx, W, H) => {
    ctx.fillStyle = '#10151c'; ctx.fillRect(0, 0, W, H);
    header(ctx, W, 'Lượt chạy đang chạy', String((runs || []).length), '#1b2430', '#59f08a');
    ctx.textBaseline = 'middle';
    if (!rows.length) { ctx.fillStyle = '#6b7a8c'; ctx.font = '600 30px ' + FONT; ctx.fillText('Không có lượt nào đang chạy.', 32, 150); return; }
    ctx.font = '600 22px ' + FONT; ctx.fillStyle = '#6b7a8c';
    ctx.fillText('AGENT', 32, 112); ctx.fillText('THỜI GIAN', W * 0.58, 112); ctx.fillText('CHI PHÍ', W * 0.78, 112);
    rows.forEach(([a, dur, cost, id], i) => {
      const y = 158 + i * 58;
      ctx.fillStyle = i % 2 ? '#141b24' : '#18212c'; ctx.fillRect(16, y - 27, W - 32, 54);
      ctx.fillStyle = '#e6f0ff'; ctx.font = '600 28px ' + FONT; ctx.fillText(fitText(ctx, a, W * 0.36), 32, y - 8);
      ctx.fillStyle = '#6b7a8c'; ctx.font = '500 15px ui-monospace, Menlo, monospace'; ctx.fillText(fitText(ctx, id, W * 0.36), 32, y + 17);
      ctx.fillStyle = '#59f08a'; ctx.font = '700 28px ui-monospace, Menlo, monospace'; ctx.fillText(dur, W * 0.58, y);
      ctx.fillStyle = '#f2c76e'; ctx.fillText(cost, W * 0.78, y);
    });
  });
}

// ------------------------------------------------------------ bảng "Cần bạn" ở lối vào: approval + câu hỏi
export function drawNeeds(board, approvals, questions, names) {
  const ap = (approvals || []).slice(0, 4), qs = (questions || []).slice(0, 3);
  const sig = JSON.stringify([ap.map((a) => [a.id, a.status, Math.floor((a.wait_s || 0) / 60)]), qs.map((q) => [q.id, q.status]), approvals.length, questions.length]);
  board.draw(sig, (ctx, W, H) => {
    board.hits = []; // vùng bấm (px canvas) → mở bảng duyệt / câu hỏi 3D
    ctx.fillStyle = '#fffaf2'; ctx.fillRect(0, 0, W, H);
    const total = (approvals || []).length + (questions || []).length;
    header(ctx, W, 'Cần bạn', total ? total + ' việc' : 'Không có gì', total ? '#c4483a' : '#4d7c43', '#ffffff');
    let y = 118;
    ctx.textBaseline = 'middle';
    const sec = (t) => { ctx.fillStyle = '#73726c'; ctx.font = '700 24px ' + FONT; ctx.fillText(t, 30, y); y += 34; };
    if (!total) { ctx.fillStyle = '#73726c'; ctx.font = '600 30px ' + FONT; ctx.fillText('Mọi agent đang tự làm được việc.', 30, y + 20); return; }
    if (ap.length) {
      sec('PHÊ DUYỆT (' + approvals.length + ')');
      for (const a of ap) {
        const hot = a.status === 'waiting_human';
        board.hits.push({ x: 22, y: y - 6, w: W - 44, h: 86, kind: 'approval', id: a.id });
        ctx.fillStyle = hot ? '#fde2dc' : '#fbf1e1'; roundRect(ctx, 22, y - 6, W - 44, 86, 14); ctx.fill();
        ctx.fillStyle = hot ? '#c4483a' : '#a86414'; ctx.fillRect(22, y + 4, 6, 66);
        ctx.fillStyle = '#2d2a24'; ctx.font = '700 26px ' + FONT;
        const head = (names(a.agent) || a.agent || '—') + ' · ' + (a.tool || '?') + (a.klass ? ' [' + a.klass + ']' : '');
        ctx.fillText(fitText(ctx, head, W - 220), 42, y + 16);
        ctx.textAlign = 'right'; ctx.fillStyle = hot ? '#c4483a' : '#a86414'; ctx.font = '600 22px ' + FONT;
        ctx.fillText((hot ? 'chờ bạn ' : 'chờ ') + fmtDur(a.wait_s || 0), W - 40, y + 16); ctx.textAlign = 'left';
        ctx.fillStyle = '#5a5649'; ctx.font = '500 22px ui-monospace, Menlo, monospace';
        ctx.fillText(fitText(ctx, a.preview || '', W - 90), 42, y + 52);
        y += 96;
      }
      if (approvals.length > ap.length) { ctx.fillStyle = '#73726c'; ctx.font = '600 22px ' + FONT; ctx.fillText('+' + (approvals.length - ap.length) + ' phê duyệt nữa', 30, y); y += 34; }
    }
    if (qs.length && y < H - 90) {
      sec('CÂU HỎI (' + questions.length + ')');
      for (const q of qs) {
        if (y > H - 60) break;
        board.hits.push({ x: 22, y: y - 6, w: W - 44, h: 60, kind: 'question', id: q.id });
        ctx.fillStyle = '#efe7fb'; roundRect(ctx, 22, y - 6, W - 44, 60, 14); ctx.fill();
        ctx.fillStyle = '#7b5ea7'; ctx.font = '700 24px ' + FONT;
        const who = (names(q.agent) || q.agent || '—') + (q.status === 'with_manager' ? ' (điều phối viên)' : '') + ': ';
        const ww = Math.min(W * 0.45, ctx.measureText(who).width);
        ctx.fillText(fitText(ctx, who, W * 0.45), 40, y + 24);
        ctx.fillStyle = '#2d2a24'; ctx.font = '500 24px ' + FONT;
        ctx.fillText(fitText(ctx, q.text || '', W - 90 - ww), 44 + ww, y + 24);
        y += 70;
      }
    }
  });
}

// ------------------------------------------------------------ sprite có chữ (thẻ nhân vật, thẻ đỏ cần duyệt, biển trạng thái)
export class TextSprite {
  constructor(px, py, worldW, opts) {
    this.canvas = document.createElement('canvas');
    this.canvas.width = px; this.canvas.height = py;
    this.tex = tex(this.canvas);
    const o = opts || {};
    this.sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.tex, transparent: true, depthTest: o.depthTest !== false, depthWrite: false }));
    this.sprite.scale.set(worldW, (worldW * py) / px, 1);
    this.sprite.renderOrder = o.order || 20;
    this.sig = null;
  }
  draw(sig, fn) {
    if (sig === this.sig) return;
    this.sig = sig;
    const ctx = this.canvas.getContext('2d');
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    fn(ctx, this.canvas.width, this.canvas.height);
    this.tex.needsUpdate = true;
  }
  dispose() { this.tex.dispose(); this.sprite.material.dispose(); this.sprite.removeFromParent(); }
}

// Thẻ thông tin nhân vật đang chọn (bay cạnh nhân vật).
export function drawCard(ts, info) {
  ts.draw(JSON.stringify(info), (ctx, W, H) => {
    ctx.fillStyle = 'rgba(255,253,248,0.97)'; roundRect(ctx, 6, 6, W - 12, H - 12, 28); ctx.fill();
    ctx.strokeStyle = info.color || '#c96442'; ctx.lineWidth = 6; ctx.stroke();
    ctx.fillStyle = info.color || '#c96442'; ctx.beginPath(); ctx.arc(46, 54, 14, 0, Math.PI * 2); ctx.fill();
    ctx.textBaseline = 'middle'; ctx.fillStyle = '#1f1e1d'; ctx.font = '700 38px ' + FONT;
    ctx.fillText(fitText(ctx, info.name, W - 110), 74, 54);
    ctx.fillStyle = '#73726c'; ctx.font = '500 22px ui-monospace, Menlo, monospace';
    ctx.fillText(fitText(ctx, (info.manager ? 'Điều phối viên · ' : '') + info.id, W - 90), 34, 98);
    // trạng thái (viên thuốc)
    ctx.font = '700 26px ' + FONT;
    const st = info.status, sw = ctx.measureText(st).width + 36, sc = STATUS_COLOR[info.statusKey] || '#73726c';
    ctx.fillStyle = sc; roundRect(ctx, 30, 124, sw, 44, 22); ctx.fill();
    ctx.fillStyle = '#ffffff'; ctx.fillText(st, 48, 147);
    if (info.where) { ctx.fillStyle = '#73726c'; ctx.font = '500 22px ' + FONT; ctx.fillText(fitText(ctx, info.where, W - sw - 80), 44 + sw, 147); }
    const rows = [['Vai trò', info.role], ['Team', info.team], ['Việc', info.task], ['Tool', info.tool], ['Run', info.run]];
    if (info.approval) rows.push(['Cần duyệt', info.approval]);
    let y = 200;
    for (const [k, v] of rows) {
      ctx.fillStyle = '#73726c'; ctx.font = '600 23px ' + FONT; ctx.fillText(k, 34, y);
      ctx.fillStyle = k === 'Cần duyệt' ? '#c4483a' : '#2d2a24';
      ctx.font = (k === 'Run' || k === 'Tool' ? '500 22px ui-monospace, Menlo, monospace' : '500 25px ' + FONT);
      if (k === 'Việc') {
        const ln = wrap(ctx, v || '—', W - 190, 2);
        ln.forEach((l, i) => ctx.fillText(l, 160, y + i * 30));
        y += 30 * Math.max(0, ln.length - 1);
      } else ctx.fillText(fitText(ctx, v || '—', W - 190), 160, y);
      y += 44;
    }
    // nút trên thẻ (giai đoạn 2): Duyệt / Trả lời / Chat — vùng bấm lưu ở ts.hits (px canvas)
    ts.hits = [];
    const acts = info.actions || [];
    if (acts.length) {
      const bh = 58, gap = 12, bw = (W - 60 - gap * (acts.length - 1)) / acts.length, by = H - bh - 26;
      acts.forEach((a, i) => {
        const bx = 30 + i * (bw + gap);
        ctx.fillStyle = a.kind === 'danger' ? '#c4483a' : a.kind === 'ask' ? '#7b5ea7' : '#c96442';
        roundRect(ctx, bx, by, bw, bh, 18); ctx.fill();
        ctx.fillStyle = '#ffffff'; ctx.font = '700 25px ' + FONT; ctx.textAlign = 'center';
        ctx.fillText(fitText(ctx, a.label, bw - 16), bx + bw / 2, by + bh / 2 + 1); ctx.textAlign = 'left';
        ts.hits.push({ x: bx, y: by, w: bw, h: bh, key: a.key });
      });
    }
  });
}

// Thẻ đỏ trên đầu agent có approval đang chờ.
export function drawApprovalTag(ts, a, waitingHuman) {
  ts.draw(JSON.stringify([a.tool, a.klass, a.preview, waitingHuman]), (ctx, W, H) => {
    ctx.fillStyle = waitingHuman ? '#c4483a' : '#c47a1a'; roundRect(ctx, 4, 4, W - 8, H - 8, 20); ctx.fill();
    ctx.fillStyle = '#ffffff'; ctx.textBaseline = 'middle'; ctx.font = '800 30px ' + FONT;
    ctx.fillText(fitText(ctx, (waitingHuman ? '✋ Chờ bạn duyệt · ' : 'Chờ duyệt · ') + (a.tool || '?') + (a.klass ? ' [' + a.klass + ']' : ''), W - 40), 22, 40);
    ctx.font = '500 24px ui-monospace, Menlo, monospace'; ctx.fillStyle = '#ffe9e4';
    ctx.fillText(fitText(ctx, a.preview || '', W - 40), 22, 86);
  });
}

// Biển lớn giữa cảnh: hết phiên / mất kết nối.
export function drawSign(ts, kind) {
  ts.draw(kind, (ctx, W, H) => {
    const exp = kind === 'expired';
    ctx.fillStyle = exp ? '#c4483a' : '#c47a1a'; roundRect(ctx, 8, 8, W - 16, H - 16, 36); ctx.fill();
    ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 8; roundRect(ctx, 24, 24, W - 48, H - 48, 26); ctx.stroke();
    ctx.fillStyle = '#ffffff'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = '800 64px ' + FONT;
    ctx.fillText(exp ? 'Phiên đăng nhập đã hết' : 'Mất kết nối với hub', W / 2, H * 0.4);
    ctx.font = '600 36px ' + FONT;
    ctx.fillText(exp ? 'Chạy  cfharness web --open  để đăng nhập lại' : 'Đang thử kết nối lại…', W / 2, H * 0.7);
    ctx.textAlign = 'left';
  });
}
