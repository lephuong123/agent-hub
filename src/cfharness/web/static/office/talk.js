/* Văn phòng 3D — nói chuyện với một agent trong chế độ đi bộ (đến gần agent, bấm E). Bảng hội thoại phủ trước camera (như chat trên
 * điện thoại), phong cách office. Gửi tin dùng ĐÚNG action run sẵn có qua POST /office/do/<name> (post_guard: CSRF + khoá phiên `k`):
 *   agent đang chạy (working / chờ duyệt / đang hỏi, có run_id) → `steer` vào run hiện tại;
 *   agent rảnh + đã có run do hội thoại này bắt đầu (trong lần mở trang) → `run_continue` run đó (giữ ngữ cảnh, run MỚI);
 *   còn lại → `run_start` một run mới. (Không tự tiếp một run cũ không do hội thoại này bắt đầu — vd. run của task.)
 * Server trả need:"confirm" ("Sẽ bắt đầu run mới cho <agent>…") → hộp xác nhận, NGƯỜI bấm mới gửi confirm=yes: MỖI lần bắt đầu run mới
 * (run_start / run_continue — tốn tiền) đều hỏi lại; steer chỉ hỏi lần đầu cho MỖI run (confirmKey gắn run_id). Câu trả lời: SSE /sse/run/{id} sẵn có — chỉ đọc khung `delta` (chữ thô); khung `item` mang HTML → BỎ QUA, không đọc.
 * Chữ của agent là DỮ LIỆU: chỉ fillText (bản sr-only: textContent qua Panel). Gõ chữ qua <textarea> thật (IME tiếng Việt). */
import { FONT, roundRect } from './util.js';
import { Panel, COLORS as C, wrapLines, clean } from './ui.js';

const MAX_TEXT = 8000, MAX_REPLY = 20000;
// Thứ tự vẽ: bảng nói chuyện DƯỚI nền mờ (1000) và bảng modal (1001) của Modals (flows.js) — bảng duyệt/trả lời mở từ đây luôn nằm trên.
export const TALK_ORDER = 990;
export const RUNNING = new Set(['working', 'waiting', 'waiting_human', 'asking']);

// Thuần: chọn action cho tin kế tiếp. a = {id, status, run_id}; sess = {lastRun}.
export function talkPlan(a, sess, text) {
  if (RUNNING.has(a.status) && a.run_id) return { name: 'steer', fields: { id: a.run_id, text } };
  if (sess && sess.lastRun) return { name: 'run_continue', fields: { id: sess.lastRun, prompt: text } };
  return { name: 'run_start', fields: { agent: a.id, prompt: text } };
}
// Dòng báo trước khi gửi (hiện trên bảng): người biết tin sẽ chen vào run đang chạy hay bắt đầu run MỚI (tốn tiền).
export function talkNotice(name, agentName) {
  if (name === 'steer') return 'Tin sẽ chen vào run đang chạy của ' + agentName + '.';
  if (name === 'run_continue') return 'Tin sẽ bắt đầu run mới cho ' + agentName + ' (tiếp phiên vừa rồi) — có thể tốn tiền.';
  return 'Tin sẽ bắt đầu run mới cho ' + agentName + ' — có thể tốn tiền; budget và giới hạn của agent vẫn áp dụng.';
}

// Thuần: khoá "đã xác nhận" để khỏi hỏi lại. null = luôn hỏi (run_start / run_continue bắt đầu run MỚI, có thể tốn tiền).
export function confirmKey(plan) { return plan && plan.name === 'steer' && plan.fields && plan.fields.id ? 'steer:' + plan.fields.id : null; }

// Thuần: một khung SSE /sse/run vào trạng thái trả lời r = {pending, mode, seq}. Trả 'dirty' (vẽ lại) | 'end' | null (bỏ qua).
// steer: luồng phát lại cả run từ đầu → khung `item` kind "steer" (user.message — một tin của người) mở đoạn mới; đoạn cuối = trả lời sau
// tin của bạn. Khung `refresh` (run.completed, duyệt, câu hỏi…) KHÔNG xoá gì — trước đây xoá → mất câu trả lời khi run kết thúc.
// KHÔNG đọc trường html của khung `item` (chỉ kind/seq).
// `hello` mang epoch của daemon: epoch đổi so với lần trước (daemon khởi động lại) → daemon phát lại cả run từ đầu (since=0)
// → bỏ đoạn đang dở + seq cũ để khỏi nhân đôi chữ.
export function replyFrame(r, f) {
  if (!f || typeof f !== 'object') return null;
  if (f.t === 'hello') {
    if (f.epoch) { if (r.epoch && r.epoch !== f.epoch) { r.pending = ''; r.seq = 0; } r.epoch = String(f.epoch); }
    return null;
  }
  if (typeof f.seq === 'number' && f.seq > (r.seq || 0)) r.seq = f.seq;
  if (f.t === 'delta' && f.k === 'text') { r.pending = ((r.pending || '') + String(f.text || '')).slice(-MAX_REPLY); return 'dirty'; }
  if (f.t === 'item' && f.kind === 'steer' && r.mode === 'steer') { r.pending = ''; return 'dirty'; }
  if (f.t === 'item' && f.kind === 'msg' && r.pending && !r.pending.endsWith('\n')) { r.pending += '\n'; return 'dirty'; }
  if (f.t === 'end') return 'end';
  return null;
}
// Thuần: chờ trước lần nối lại thứ n (0, 1, …) khi luồng trả lời lỗi — lùi dần 1 s → 30 s; null = thôi, báo luồng đã đóng.
export const RETRY_MAX = 6;
export function retryDelay(n) { return n >= RETRY_MAX ? null : Math.min(30000, 1000 * 2 ** n); }
// Thuần: chỉ khung có nội dung (delta/item) mới xoá bộ đếm thử lại — `hello` thì không, kẻo nối được rồi rớt ngay sẽ thử lại mãi mỗi 1 s.
export function resetsRetry(f) { return !!f && (f.t === 'delta' || f.t === 'item'); }
// Thuần: URL luồng trả lời; nối lại kèm since + epoch (khác epoch → daemon phát lại từ đầu thay vì bỏ sót/nhân đôi).
export function streamUrl(runId, s) {
  const q = [];
  if (s && s.seq) q.push('since=' + encodeURIComponent(String(s.seq)));
  if (s && s.seq && s.epoch) q.push('epoch=' + encodeURIComponent(String(s.epoch)));
  return '/sse/run/' + encodeURIComponent(runId) + (q.length ? '?' + q.join('&') : '');
}

export function createTalk(o) {
  // o: {api, srRoot, camera, toast(msg, ok), onExpired(), agent(id) → {id, name, status, run_id, approval, question} | null,
  //     ask(d) → Promise<bool> (hộp xác nhận, true khi NGƯỜI bấm Đồng ý), openApproval(id), openQuestion(id), onOpenChange(open, id)}
  const sessions = new Map(); // agentId → {messages, lastRun, confirmed:Set, pending, es, esRun, mode, seq, tries, retry}
  const st = { id: null, sending: false };
  const panel = new Panel({ title: 'Nói chuyện với agent', pxW: 1500, pxH: 900, srRoot: o.srRoot, modal: false, build, onEscape: () => close() });
  panel.mesh.userData.talk = true;
  panel.sr.hidden = true;
  const m = panel.mesh.material;
  m.depthTest = false; m.depthWrite = false; panel.mesh.renderOrder = TALK_ORDER; panel.mesh.visible = false;
  o.camera.add(panel.mesh);

  function fit() {
    const cam = o.camera, d = 1.6, vh = 2 * d * Math.tan((cam.fov * Math.PI) / 360), vw = vh * cam.aspect;
    const narrow = cam.aspect < 0.85;
    if (narrow && panel.W !== 760) panel.setSize(760, 1240); else if (!narrow && panel.W !== 1500) panel.setSize(1500, 900);
    const s = Math.min((0.96 * vw) / panel.W, (0.9 * vh) / panel.H);
    panel.mesh.scale.set(panel.W * s, panel.H * s, 1);
    panel.mesh.position.set(0, 0, -d);
  }
  const sess = (id) => {
    let s = sessions.get(id);
    if (!s) { s = { messages: [], lastRun: null, confirmed: new Set(), pending: null, es: null, esRun: null, mode: '', seq: 0, tries: 0, retry: null }; sessions.set(id, s); }
    return s;
  };

  // ---------------------------------------------------------------- luồng trả lời (SSE run)
  function stream(id, runId, mode) {
    const s = sess(id);
    if (s.esRun === runId && (s.es || s.retry)) return;
    stop(s, false);
    s.esRun = runId; s.mode = mode; s.pending = ''; s.seq = 0; s.epoch = null; s.tries = 0;
    connect(s, runId);
  }
  function connect(s, runId) {
    // nối lại: tiếp từ seq đã thấy (since) → không phát lại, không nhân đôi chữ
    const es = new EventSource(streamUrl(runId, s), { withCredentials: true });
    s.es = es;
    es.onmessage = (ev) => {
      if (s.es !== es) return;
      let f; try { f = JSON.parse(ev.data); } catch (_) { return; }
      if (resetsRetry(f)) s.tries = 0;
      const r = replyFrame(s, f);
      if (r === 'end') stop(s, true);
      if (r) panel.dirty = true;
    };
    es.onerror = () => {
      if (s.es !== es) return;
      es.close(); s.es = null;
      const wait = retryDelay(s.tries++);
      if (wait == null) { stop(s, true); s.messages.push({ kind: 'note', text: 'Luồng trả lời đã đóng.' }); panel.dirty = true; return; }
      s.retry = setTimeout(() => { s.retry = null; if (s.esRun === runId && !s.es) connect(s, runId); }, wait);
    };
  }
  // đoạn trả lời đang dở thành một tin của agent (trước khi gửi tin mới vào cùng run)
  function commit(s) {
    if (s.pending && s.pending.trim()) s.messages.push({ kind: 'agent', text: s.pending });
    if (s.pending != null) s.pending = '';
  }
  function stop(s, keep) {
    if (s.retry) { clearTimeout(s.retry); s.retry = null; }
    if (s.es) { s.es.close(); s.es = null; }
    if (keep && s.pending && s.pending.trim()) s.messages.push({ kind: 'agent', text: s.pending });
    s.pending = null; s.esRun = null;
    if (s.messages.length > 100) s.messages.splice(0, s.messages.length - 100);
  }

  // ---------------------------------------------------------------- gửi
  async function send() {
    const el = panel.get('input'), a = st.id && o.agent(st.id);
    if (!el || !a || st.sending) return;
    const text = el.value.trim();
    if (!text) return;
    if (text.length > MAX_TEXT) { o.toast('Tin quá dài (tối đa ' + MAX_TEXT + ' ký tự)', false); return; }
    const s = sess(a.id), plan = talkPlan(a, s, text), url = '/office/do/' + plan.name, ck = confirmKey(plan);
    st.sending = true; panel.busy = true; panel.dirty = true;
    let res = await o.api.post(url, ck && s.confirmed.has(ck) ? Object.assign({ confirm: 'yes' }, plan.fields) : plan.fields);
    if (!res.error && res.data && res.data.need === 'confirm') {
      // hỏi (thông điệp do server soạn) — chỉ người bấm "Đồng ý" mới gửi confirm=yes; run mới: lần nào cũng hỏi, steer: một lần mỗi run
      const ok = await o.ask(res.data);
      if (ok) { if (ck) s.confirmed.add(ck); res = await o.api.post(url, Object.assign({ confirm: 'yes' }, plan.fields)); } else res = null;
    }
    st.sending = false; panel.busy = false; panel.dirty = true;
    if (!res) { setTimeout(() => panel.focus('input'), 0); return; }
    if (res.status === 401) { o.onExpired(); return; }
    if (res.error) { o.toast(res.error, false); return; }
    const d = res.data || {};
    if (!d.ok) { o.toast('Phản hồi không rõ từ hub', false); return; }
    el.value = '';
    if (d.run_id && s.esRun === d.run_id) commit(s); // tin mới vào run đang nghe: chốt câu trả lời trước đó, mở đoạn mới
    s.messages.push({ kind: 'user', text, steered: plan.name === 'steer' });
    if (plan.name !== 'steer' && d.run_id) s.lastRun = d.run_id;
    if (d.run_id) stream(a.id, d.run_id, plan.name === 'steer' ? 'steer' : 'run');
    const sc = panel.scroll.hist; if (sc) sc.stick = true;
    setTimeout(() => { panel.draw(); panel.focus('input'); }, 0);
  }

  // ---------------------------------------------------------------- mở / đóng
  function open(id) {
    if (!o.agent(id)) return;
    if (st.id && st.id !== id) close();
    st.id = id; panel.sr.hidden = false; panel.mesh.visible = true; panel.dirty = true;
    const s = sess(id), a = o.agent(id);
    // agent đang chạy run mình đã gửi trước đó: nghe tiếp
    if (a && a.run_id && s.lastRun === a.run_id && !s.es && !s.retry) stream(id, a.run_id, 'run');
    fit();
    o.onOpenChange(true, id);
    setTimeout(() => { panel.draw(); panel.focus('input'); }, 0);
  }
  function close() {
    if (!st.id) return;
    const id = st.id;
    st.id = null; panel.sr.hidden = true; panel.mesh.visible = false; panel.dirty = true;
    if (document.activeElement && panel.sr.contains(document.activeElement)) document.activeElement.blur();
    o.onOpenChange(false, id); // luồng trả lời vẫn chạy nền — mở lại thấy câu trả lời
  }

  // ---------------------------------------------------------------- vẽ
  function bubble(ctx, kind, text, w, who) {
    const small = kind === 'note';
    const font = small ? '500 22px ' + FONT : '500 26px ' + FONT, lh = small ? 30 : 35;
    ctx.font = font;
    const maxW = small ? w - 20 : w * 0.76;
    const lines = wrapLines(ctx, clean(text) || '…', maxW - 40, 60);
    const bw = Math.min(maxW, Math.max(...lines.map((l) => ctx.measureText(l).width), 60) + 40);
    const head = kind === 'agent' || kind === 'pending' ? 28 : 0, h = lines.length * lh + 24 + head + 12;
    return {
      h,
      draw: (c, x, y, ww) => {
        c.font = font; c.textBaseline = 'top';
        if (kind === 'user') {
          const bx = x + ww - bw;
          c.fillStyle = '#f7ebe4'; roundRect(c, bx, y, bw, h - 12, 20); c.fill();
          c.fillStyle = C.text; lines.forEach((l, i) => c.fillText(l, bx + 20, y + 12 + i * lh));
        } else if (kind === 'agent' || kind === 'pending') {
          c.fillStyle = '#ffffff'; roundRect(c, x, y, bw, h - 12, 20); c.fill();
          c.strokeStyle = kind === 'pending' ? C.accent : C.line; c.lineWidth = 3; c.stroke();
          c.fillStyle = C.accent; c.font = '700 19px ' + FONT; c.fillText(who + (kind === 'pending' ? ' · đang trả lời…' : ''), x + 20, y + 10);
          c.font = font; c.fillStyle = C.text;
          lines.forEach((l, i) => c.fillText(l + (kind === 'pending' && i === lines.length - 1 ? ' ▍' : ''), x + 20, y + 12 + head + i * lh));
        } else {
          c.fillStyle = C.muted; lines.forEach((l, i) => c.fillText((i ? '   ' : '· ') + l, x + 10, y + 6 + i * lh));
        }
      },
    };
  }

  function build(ui) {
    const W = ui.W, H = ui.H, ctx = ui.ctx, pad = 28, narrow = W < 1000;
    const a = st.id && o.agent(st.id);
    ctx.fillStyle = 'rgba(0,0,0,0)'; ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = C.bg; roundRect(ctx, 10, 10, W - 20, H - 20, 26); ctx.fill();
    ctx.strokeStyle = '#c96442'; ctx.lineWidth = 4; ctx.stroke();
    if (!a) { ui.heading('h', pad + 8, 54, 'Agent không còn trong văn phòng', 34, C.text, W - 2 * pad); ui.button('close', W - 212, 22, 176, 60, '✕ Đóng', { onClick: close }); return; }
    const s = sess(a.id), name = a.name || a.id;
    ui.heading('h', pad + 8, 52, '💬 ' + name, narrow ? 32 : 38, C.text, W - (narrow ? 2 * pad + 190 : 600));
    ui.text(pad + 8, 92, 'Trạng thái: ' + (a.statusText || a.status || '—') + (a.run_id ? ' · run ' + a.run_id : ''), '500 22px ' + FONT, C.muted, W - 2 * pad - 200);
    ui.button('close', W - pad - 176, 22, 176, 60, '✕ Đóng', { sr: 'Đóng hội thoại (Esc)', onClick: close });
    // agent đang chờ duyệt / đang hỏi: nút tới bảng duyệt / trả lời sẵn có
    let bx = W - pad - 176 - 16;
    if (a.question && o.openQuestion) { bx -= 230; ui.button('ansq', bx, 22, 230, 60, '❓ Trả lời…', { kind: 'primary', onClick: () => o.openQuestion(a.question) }); bx -= 16; }
    if (a.approval && o.openApproval) { bx -= 210; ui.button('ap', bx, 22, 210, 60, '🛡 Duyệt…', { kind: 'danger', onClick: () => o.openApproval(a.approval) }); }
    const top = 122, inputH = narrow ? 170 : 140, histH = H - top - inputH - 84, w = W - 2 * pad;
    const items = s.messages.map((mm) => bubble(ctx, mm.kind, mm.text, w, name));
    if (s.pending != null) items.push(bubble(ctx, 'pending', s.pending, w, name));
    if (!items.length) items.push(bubble(ctx, 'note', 'Nhắn cho ' + name + ' bên dưới (Enter gửi, Shift+Enter xuống dòng). Esc để đóng.', w));
    const srHist = s.messages.slice(-10).map((mm) => (mm.kind === 'user' ? 'Bạn: ' : mm.kind === 'agent' ? name + ': ' : '') + mm.text).join('\n');
    ui.scroll('hist', pad, top, w, histH, items, 'Hội thoại với ' + name, srHist, { stick: true, bg: '#fbf8f1' });
    const plan = talkPlan(a, s, '');
    const line = st.sending ? 'Đang gửi…' : talkNotice(plan.name, name);
    ui.text(pad + 6, top + histH + 30, line, '600 21px ' + FONT, plan.name === 'steer' ? C.muted : C.accent, w, 'notice');
    const fy = H - inputH - 26, sendW = narrow ? 130 : 170;
    ui.field('input', pad, fy, w - sendW - 20, inputH, {
      label: 'Tin nhắn cho ' + name + ' (Enter gửi, Shift+Enter xuống dòng)', rows: 3, maxLength: MAX_TEXT, fontSize: 26,
      placeholder: 'Nhắn cho ' + name + '…', onKey: (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); send(); }
      },
    });
    ui.button('send', W - pad - sendW, fy, sendW, inputH, 'Gửi', { kind: 'primary', disabled: st.sending, onClick: send });
  }

  function update() { if (st.id) panel.draw(); }
  return { panel, open, close, update, fit, send, get isOpen() { return !!st.id; }, get agentId() { return st.id; } };
}
