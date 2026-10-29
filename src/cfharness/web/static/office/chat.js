/* Văn phòng 3D — chat với điều phối viên trên MÀN HÌNH LỚN ở phòng điều phối viên (giai đoạn 2).
 * Lịch sử: GET /office/chat?c=… ; gửi: POST /manager/send (X-Ahub-Fetch, CSRF + khoá phiên — như web 2D); trực tiếp: SSE /sse/office/chat?c=…
 * (delta / reply / action / question / run_end / tick — KHÔNG HTML). Chữ điều phối viên là DỮ LIỆU: chỉ fillText.
 * Gõ chữ qua <textarea> THẬT (sr-only, nhận focus) → IME tiếng Việt chạy bình thường; canvas vẽ lại theo `input`.
 * Enter gửi (trừ khi đang ghép chữ IME), Shift+Enter xuống dòng. */
import * as THREE from '../vendor/three-0.169.0/three.module.min.js';
import { FONT, roundRect } from './util.js';
import { Panel, COLORS as C, wrapLines, clean } from './ui.js';
import { TextSprite } from './boards.js';

const MAX_TEXT = 8000;

export function createChat(o) {
  // o: {api, srRoot, toast(msg, ok), onExpired(), managerActor(), onOpenChange(open)}
  const st = { open: false, available: null, managerId: null, managerName: '', conv: null, convs: [], messages: [], pending: new Map(), showList: false, sending: false, loading: false, err: '' };
  let es = null, esConv = undefined, retryT = 0;

  const panel = new Panel({ title: 'Chat với điều phối viên', pxW: 1600, pxH: 914, srRoot: o.srRoot, modal: false, build, onEscape: () => close() });
  panel.mesh.userData.chat = true;
  panel.sr.hidden = true;

  // bong bóng lời nói trên đầu điều phối viên
  const speech = new TextSprite(720, 200, 2.6, { depthTest: false, order: 26 });
  speech.sprite.visible = false;

  let worldRef = null, overlayCam = null;
  function mount(world) {
    worldRef = world;
    world.group.add(speech.sprite);
    if (overlayCam) return; // đang phủ màn hình (điện thoại): giữ nguyên
    const a = world.chatAnchor;
    if (!a) return;
    panel.mesh.position.set(a.x, a.y, a.z);
    panel.mesh.rotation.set(0, a.rotY, 0);
    panel.mesh.scale.set(a.w, a.h, 1);
    world.group.add(panel.mesh);
    panel.dirty = true;
  }

  // Điện thoại (màn dọc): màn hình chat phủ trước camera, bố cục hẹp; màn ngang: treo trên vách phòng điều phối viên.
  function setOverlay(camera) {
    const m = panel.mesh.material;
    if (camera) {
      overlayCam = camera;
      panel.setSize(760, 1240);
      m.depthTest = false; m.depthWrite = false; panel.mesh.renderOrder = 1001;
      camera.add(panel.mesh); panel.mesh.rotation.set(0, 0, 0);
      fitOverlay();
    } else {
      overlayCam = null;
      panel.setSize(1600, 914);
      m.depthTest = true; m.depthWrite = true; panel.mesh.renderOrder = 0;
      if (worldRef) mount(worldRef);
    }
    m.needsUpdate = true;
  }
  function fitOverlay() {
    if (!overlayCam) return;
    const d = 1.6, vh = 2 * d * Math.tan((overlayCam.fov * Math.PI) / 360), vw = vh * overlayCam.aspect;
    const s = Math.min((0.98 * vw) / panel.W, (0.94 * vh) / panel.H);
    panel.mesh.scale.set(panel.W * s, panel.H * s, 1);
    panel.mesh.position.set(0, 0, -d);
  }

  function frame() {
    const c = new THREE.Vector3(), n = new THREE.Vector3(0, 0, 1);
    panel.mesh.getWorldPosition(c);
    n.applyQuaternion(panel.mesh.getWorldQuaternion(new THREE.Quaternion())).normalize();
    return { center: c, normal: n, w: panel.mesh.scale.x, h: panel.mesh.scale.y };
  }

  // ---------------------------------------------------------------- dữ liệu
  async function load(conv) {
    st.loading = true; panel.dirty = true;
    const res = await o.api.getJSON('/office/chat' + (conv ? '?c=' + encodeURIComponent(conv) : ''));
    st.loading = false; panel.dirty = true;
    if (res.status === 401) { o.onExpired(); return; }
    if (res.error) { st.err = res.error; return; }
    const d = res.data || {};
    st.err = '';
    st.available = d.available !== false;
    st.managerId = d.manager_id || null;
    st.conv = d.conversation_id || conv || null;
    st.convs = Array.isArray(d.conversations) ? d.conversations : [];
    st.messages = (Array.isArray(d.messages) ? d.messages : []).slice(-50);
    st.pending.clear();
    const s = panel.scroll.hist; if (s) s.stick = true;
    connect();
  }

  function connect() {
    if (!st.open) return;
    if (es && esConv === st.conv) return;
    if (es) es.close();
    esConv = st.conv;
    es = new EventSource('/sse/office/chat' + (st.conv ? '?c=' + encodeURIComponent(st.conv) : ''), { withCredentials: true });
    es.onmessage = (m) => { let f; try { f = JSON.parse(m.data); } catch (_) { return; } onFrame(f); };
    es.onerror = () => {
      if (es) es.close(); es = null; esConv = undefined;
      clearTimeout(retryT);
      retryT = setTimeout(async () => {
        if (!st.open) return;
        const r = await o.api.getJSON('/office/chat' + (st.conv ? '?c=' + encodeURIComponent(st.conv) : ''));
        if (r.status === 401) { o.onExpired(); return; }
        connect();
      }, 3000);
    };
  }

  function mine(conv) { return !conv || !st.conv || conv === st.conv; }
  function onFrame(f) {
    if (!f || typeof f !== 'object') return;
    if (f.t === 'delta' && f.run) {
      if (!st.pending.has(f.run) && st.messages.some((m) => m.run_id === f.run && m.kind === 'manager')) return;
      st.pending.set(f.run, (st.pending.get(f.run) || '') + String(f.text || ''));
    } else if (f.t === 'reply' && mine(f.conv)) {
      st.pending.delete(f.run);
      st.messages.push({ kind: 'manager', text: String(f.text || ''), run_id: f.run, notify: !!f.notify });
    } else if (f.t === 'action') {
      st.messages.push({ kind: 'action', text: (f.tool ? f.tool + ' — ' : '') + String(f.summary || ''), run_id: f.run, ok: f.ok !== false });
    } else if (f.t === 'question' && mine(f.conv)) {
      if (f.answered) st.messages.push({ kind: 'note', text: 'Câu hỏi đã được trả lời.', run_id: null });
      else st.messages.push({ kind: 'question', text: 'Điều phối viên đặt một câu hỏi cho bạn — bấm "Trả lời" dưới đây hoặc xem bảng "Cần bạn".', run_id: null, id: f.id, status: 'open' });
    } else if (f.t === 'run_end') {
      const left = st.pending.get(f.run);
      st.pending.delete(f.run);
      if (left && !st.messages.some((m) => m.run_id === f.run && m.kind === 'manager')) st.messages.push({ kind: 'manager', text: left, run_id: f.run });
      if (f.outcome && f.outcome !== 'completed') st.messages.push({ kind: 'note', text: 'Lượt xử lý kết thúc: ' + f.outcome, run_id: f.run });
    } else return;
    if (st.messages.length > 200) st.messages.splice(0, st.messages.length - 200);
    panel.dirty = true;
  }

  async function send() {
    const el = panel.get('input');
    if (!el || st.sending) return;
    const text = el.value.trim();
    if (!text) return;
    if (text.length > MAX_TEXT) { o.toast('Tin quá dài (tối đa ' + MAX_TEXT + ' ký tự)', false); return; }
    st.sending = true; panel.busy = true; panel.dirty = true;
    const res = await o.api.post('/manager/send', { text, c: st.conv || '' });
    st.sending = false; panel.busy = false; panel.dirty = true;
    if (res.status === 401) { o.onExpired(); return; }
    if (res.error) { o.toast(res.error, false); return; }
    const d = res.data || {};
    el.value = '';
    st.messages.push({ kind: 'user', text, run_id: d.run_id || null, steered: !!d.steered });
    if (d.run_id && !d.steered) st.pending.set(d.run_id, '');
    const s = panel.scroll.hist; if (s) s.stick = true;
    if (d.conversation_id && d.conversation_id !== st.conv) { st.conv = d.conversation_id; connect(); }
    setTimeout(() => { panel.draw(); panel.focus('input'); }, 0);
  }

  function newConv() {
    st.conv = null; st.messages = []; st.pending.clear(); st.showList = false;
    if (es) { es.close(); es = null; esConv = undefined; }
    connect();
    panel.dirty = true;
    setTimeout(() => { panel.draw(); panel.focus('input'); }, 0);
  }

  // ---------------------------------------------------------------- mở / đóng
  async function open() {
    if (st.open) { panel.draw(); panel.focus('input'); return; }
    st.open = true; panel.sr.hidden = false; panel.dirty = true;
    if (o.camera && o.camera.aspect < 0.85) setOverlay(o.camera);
    o.onOpenChange(true);
    await load(st.conv);
    setTimeout(() => { panel.draw(); panel.focus('input'); }, 0);
  }
  function close() {
    if (!st.open) return;
    st.open = false; st.showList = false; panel.sr.hidden = true; panel.dirty = true;
    if (overlayCam) setOverlay(null);
    if (es) { es.close(); es = null; esConv = undefined; }
    clearTimeout(retryT);
    if (document.activeElement && panel.sr.contains(document.activeElement)) document.activeElement.blur();
    speech.sprite.visible = false;
    o.onOpenChange(false);
  }

  // ---------------------------------------------------------------- vẽ màn hình
  function bubble(ctx, kind, text, w, extra) {
    // trả item {h, draw} cho hộp cuộn
    const font = kind === 'action' || kind === 'note' ? '500 23px ' + FONT : '500 27px ' + FONT;
    const lh = kind === 'action' || kind === 'note' ? 30 : 36;
    ctx.font = font;
    const maxW = kind === 'user' || kind === 'manager' || kind === 'pending' ? w * 0.74 : w - 20;
    const lines = wrapLines(ctx, clean(text) || (kind === 'pending' ? '…' : ''), maxW - 40, 60);
    const bw = Math.min(maxW, Math.max(...lines.map((l) => ctx.measureText(l).width), 60) + 40);
    const head = kind === 'manager' || kind === 'pending' ? 30 : 0;
    const h = lines.length * lh + 24 + head + 14;
    return {
      h,
      draw: (c, x, y, ww) => {
        c.font = font; c.textBaseline = 'top';
        if (kind === 'user') {
          const bx = x + ww - bw;
          c.fillStyle = '#f7ebe4'; roundRect(c, bx, y, bw, h - 14, 20); c.fill();
          c.fillStyle = C.text; lines.forEach((l, i) => c.fillText(l, bx + 20, y + 12 + i * lh));
          if (extra && extra.steered) { c.font = '500 18px ' + FONT; c.fillStyle = C.muted; c.fillText('đã chen vào lượt đang chạy', bx + 20, y + h - 30); }
        } else if (kind === 'manager' || kind === 'pending') {
          c.fillStyle = '#ffffff'; roundRect(c, x, y, bw, h - 14, 20); c.fill();
          c.strokeStyle = kind === 'pending' ? C.accent : C.line; c.lineWidth = 3; c.stroke();
          c.fillStyle = C.accent; c.font = '700 20px ' + FONT; c.fillText(kind === 'pending' ? 'Điều phối viên · đang trả lời…' : 'Điều phối viên' + (extra && extra.notify ? ' · thông báo' : ''), x + 20, y + 10);
          c.font = font; c.fillStyle = C.text;
          lines.forEach((l, i) => c.fillText(l + (kind === 'pending' && i === lines.length - 1 ? ' ▍' : ''), x + 20, y + 12 + head + i * lh));
        } else {
          const col = kind === 'action' ? (extra && extra.ok === false ? C.danger : C.ok) : kind === 'question' ? '#7b5ea7' : C.muted;
          c.fillStyle = col;
          lines.forEach((l, i) => c.fillText((i === 0 ? (kind === 'action' ? '📋 ' : kind === 'question' ? '❓ ' : '· ') : '   ') + l, x + 10, y + 6 + i * lh));
        }
      },
    };
  }

  function build(ui) {
    const W = ui.W, H = ui.H, ctx = ui.ctx;
    ctx.fillStyle = '#1d2230'; ctx.fillRect(0, 0, W, H);
    if (!st.open) {
      // màn hình chờ (khi chưa mở): mời bấm
      ctx.fillStyle = '#e9ecf2'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.font = '800 76px ' + FONT; ctx.fillText('💬 Chat với điều phối viên', W / 2, H / 2 - 40);
      ctx.font = '600 38px ' + FONT; ctx.fillStyle = '#9aa3b5'; ctx.fillText('Bấm vào màn hình để mở', W / 2, H / 2 + 50);
      ctx.textAlign = 'left';
      return;
    }
    const pad = 28;
    ctx.fillStyle = C.bg; roundRect(ctx, 12, 12, W - 24, H - 24, 26); ctx.fill();
    const mgr = o.managerActor();
    st.managerName = (mgr && (mgr.agent.name || mgr.id)) || st.managerId || '';
    const narrow = W < 1000;
    const toggleList = () => { st.showList = !st.showList; panel.dirty = true; };
    let top = 100;
    if (narrow) {
      ui.heading('h', pad + 8, 50, 'Điều phối viên' + (st.managerName ? ' · ' + st.managerName : ''), 34, C.text, W - 2 * pad - 20);
      const bw = (W - 2 * pad - 20) / 3;
      ui.button('new', pad, 86, bw, 58, '＋ Mới', { sr: 'Hội thoại mới', onClick: newConv });
      ui.button('list', pad + bw + 10, 86, bw, 58, (st.showList ? '▴' : '▾') + ' Cũ', { sr: 'Hội thoại cũ', pressed: st.showList, onClick: toggleList });
      ui.button('close', pad + 2 * (bw + 10), 86, bw, 58, '✕ Đóng', { onClick: close });
      top = 160;
    } else {
      ui.heading('h', pad + 8, 52, 'Điều phối viên' + (st.managerName ? ' · ' + st.managerName : ''), 38, C.text, W - 760);
      ui.button('new', W - 720, 22, 230, 60, '＋ Hội thoại mới', { onClick: newConv });
      ui.button('list', W - 476, 22, 250, 60, (st.showList ? '▴' : '▾') + ' Hội thoại cũ', { pressed: st.showList, onClick: toggleList });
      ui.button('close', W - 212, 22, 176, 60, '✕ Đóng', { onClick: close });
    }
    let x0 = pad;
    if (st.showList) {
      const lw = narrow ? W - 2 * pad : 440;
      ctx.fillStyle = '#f5f1e8'; roundRect(ctx, pad, top, lw, H - top - 28, 16); ctx.fill();
      ui.text(pad + 18, top + 28, 'Hội thoại (kênh web)', '700 22px ' + FONT, C.muted, lw - 30);
      const list = st.convs.slice(0, 9);
      if (!list.length) ui.text(pad + 18, top + 76, 'Chưa có hội thoại nào.', '500 22px ' + FONT, C.muted, lw - 30);
      list.forEach((cv, i) => {
        const label = clean(cv.title || cv.id || '—');
        ui.button('cv' + i, pad + 12, top + 54 + i * 76, lw - 24, 64, label, {
          pressed: cv.id === st.conv, font: '600 22px ' + FONT, sr: 'Mở hội thoại: ' + (cv.title || cv.id),
          onClick: () => { st.showList = false; st.conv = cv.id; if (es) { es.close(); es = null; esConv = undefined; } load(cv.id); },
        });
      });
      if (narrow) return; // màn hẹp: danh sách phủ toàn bộ, chọn xong mới thấy lịch sử
      x0 = pad + lw + 16;
    }
    const w = W - x0 - pad, inputH = narrow ? 170 : 150;
    const histH = H - top - inputH - 70;
    let items;
    if (st.available === false) items = [bubble(ctx, 'note', 'Chưa có điều phối viên — chỉ định một agent ở giao diện 2D (trang Điều phối).', w)];
    else if (st.loading && !st.messages.length) items = [bubble(ctx, 'note', 'Đang tải hội thoại…', w)];
    else {
      items = st.messages.map((m) => bubble(ctx, m.kind === 'user' ? 'user' : m.kind === 'manager' ? 'manager' : m.kind === 'action' ? 'action' : m.kind === 'question' ? 'question' : 'note',
        m.kind === 'action' && m.tool && !String(m.text || '').startsWith(m.tool) ? m.tool + ' — ' + (m.text || '') : (m.text || ''), w, m));
      for (const [, t] of st.pending) items.push(bubble(ctx, 'pending', t, w));
      if (!items.length) items = [bubble(ctx, 'note', 'Hội thoại mới — nhắn cho điều phối viên bên dưới (Enter để gửi, Shift+Enter xuống dòng).', w)];
    }
    if (st.err) items.push(bubble(ctx, 'note', 'Lỗi: ' + st.err, w));
    const srHist = st.messages.slice(-10).map((m) => (m.kind === 'user' ? 'Bạn: ' : m.kind === 'manager' ? 'Điều phối viên: ' : '') + (m.text || '')).join('\n');
    ui.scroll('hist', x0, top, w, histH, items, 'Lịch sử hội thoại', srHist, { stick: true, bg: '#fbf8f1' });
    const openQ = [...st.messages].reverse().find((m) => m.kind === 'question' && m.id && (!m.status || m.status === 'open' || m.status === 'with_manager'));
    if (openQ && o.openQuestion) ui.button('ansq', x0 + w - 300, top + histH + 6, 300, 44, '❓ Trả lời câu hỏi', { kind: 'primary', font: '700 21px ' + FONT, onClick: () => o.openQuestion(openQ.id) });
    const busyLine = st.sending ? 'Đang gửi…' : st.pending.size ? 'Điều phối viên đang trả lời…' : '';
    if (busyLine) ui.text(x0 + 6, top + histH + 26, busyLine, '600 22px ' + FONT, C.accent, w, null);
    const fy = H - inputH - 26;
    const sendW = narrow ? 130 : 180;
    ui.field('input', x0, fy, w - sendW - 20, inputH, {
      label: 'Tin nhắn cho điều phối viên (Enter gửi, Shift+Enter xuống dòng)', rows: 3, maxLength: MAX_TEXT, fontSize: 27,
      placeholder: 'Nhắn cho điều phối viên…  (Enter gửi · Shift+Enter xuống dòng)', disabled: st.available === false,
      onKey: (e) => {
        if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) { e.preventDefault(); send(); }
      },
    });
    ui.button('send', W - pad - sendW, fy, sendW, inputH, 'Gửi', { kind: 'primary', disabled: st.available === false || st.sending, onClick: send });
  }

  // bong bóng lời nói + vẽ lại
  function update() {
    panel.draw();
    const mgr = o.managerActor();
    let say = '';
    if (st.open) {
      for (const [, t] of st.pending) say = t || '…';
      if (!say) { const last = [...st.messages].reverse().find((m) => m.kind === 'manager'); say = last ? last.text : ''; }
    }
    if (!say || !mgr || !mgr.pos) { speech.sprite.visible = false; return; }
    const tail = clean(say).replace(/\s+/g, ' ').slice(-120);
    speech.draw(tail, (ctx, W, H) => {
      ctx.fillStyle = '#ffffff'; roundRect(ctx, 6, 6, W - 12, H - 40, 26); ctx.fill();
      ctx.strokeStyle = C.accent; ctx.lineWidth = 5; ctx.stroke();
      ctx.beginPath(); ctx.moveTo(W / 2 - 22, H - 36); ctx.lineTo(W / 2, H - 4); ctx.lineTo(W / 2 + 22, H - 36); ctx.closePath(); ctx.fillStyle = '#ffffff'; ctx.fill();
      ctx.fillStyle = C.text; ctx.font = '600 30px ' + FONT; ctx.textBaseline = 'top';
      const lines = wrapLines(ctx, tail, W - 60, 3);
      lines.slice(0, 3).forEach((l, i) => ctx.fillText(l, 30, 24 + i * 40));
    });
    speech.sprite.visible = true;
    const a = worldRef && worldRef.chatAnchor;
    if (a && !overlayCam) {
      // bong bóng nằm cạnh mép màn hình phía điều phối viên (cùng độ sâu với màn hình → nằm trong khung nhìn chat)
      const dir = mgr.pos.z < a.z ? -1 : 1, edge = a.z + dir * a.w / 2;
      speech.sprite.scale.set(2.0, 2.0 * 200 / 720, 1);
      speech.sprite.position.set(a.x - 0.3, a.y + a.h / 2 + 0.42, edge - dir * 1.05);
    } else speech.sprite.position.set(mgr.pos.x, 2.45, mgr.pos.z);
  }

  const managerPos = () => { const m = o.managerActor(); return m && m.pos ? m.pos : null; };
  return { panel, mount, open, close, frame, update, fitOverlay, managerPos, get isOpen() { return st.open; }, get overlay() { return !!overlayCam; }, send };
}
