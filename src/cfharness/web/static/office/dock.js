/* Văn phòng 3D — "Điều khiển agent" dạng 2D: bảng DOM nổi ở góc trên trái màn hình (thay cho bảng quản lý agent vẽ trong cảnh 3D —
 * chủ hub 2026-11-12: "Điều khiển agent cần làm dạng 2D ở góc màn hình, có mọi quyền thao tác; điều khiển cả agent đang chưa làm gì;
 * trong team thì chia theo team"). Hợp đồng: docs/dev-notes/office-agent-board.md.
 *
 * - Thanh tiêu đề (luôn hiện, kể cả thu gọn): số agent, "N cần bạn", ESTOP đang bật. Thanh công cụ: ⛔ ESTOP toàn hub, lọc theo trạng thái
 *   (Tất cả / Cần bạn / Đang làm / Rảnh / Tạm dừng / Lỗi — kèm số), tìm agent.
 * - Nhóm: Điều phối viên → từng team (thứ tự team.list) → Không team. Tiêu đề nhóm bấm để thu gọn (nhớ theo trình duyệt), có chấm đếm trạng
 *   thái; nhóm team có nút dừng khẩn / cho chạy lại CẢ TEAM.
 * - Mỗi agent (cả agent rảnh): đèn, tên (bấm → camera tới nhân vật), trạng thái · vai trò, việc, tool · thời gian run · chi phí hôm nay; nút
 *   🛡 Duyệt, ❓ Trả lời, 💬 Chat (điều phối viên) / 💬 Nhắn (đang chạy) / ▶ Giao việc (rảnh — run mới), ⏸ Tạm dừng / ▶ Tiếp tục, ■ Dừng run;
 *   menu ⋯: 🗑 Xoá agent (thao tác hiếm/nguy hiểm để riêng, tránh bấm nhầm).
 * - Ghi: POST /office/do/<name> KHÔNG kèm confirm → server trả need:"confirm" (thông điệp của Action 2D) → hộp xác nhận DOM; ESTOP / xoá
 *   agent còn need:"confirm2" → tích ô "Tôi hiểu…" mới bấm được. confirm=yes / confirm2=yes CHỈ gửi từ nút do NGƯỜI bấm. Duyệt / trả lời /
 *   nhắn / giao việc mở đúng bảng 3D sẵn có (flows.openApproval / openQuestion, talk, chat) — cùng endpoint, CSRF, khoá phiên.
 * - Chữ của agent (tên, việc, tool) là DỮ LIỆU: chỉ `textContent`, không bao giờ HTML. Không style inline (CSP) — màu theo class. */
import { rosterItems, rosterSummary, agentActions, filterItems, fmtAge, ROLE_VI } from './roster.js';
import { STATUS_VI } from './actors.js';
import { fmtUsd } from './boards.js';

const WRITE = new Set(['agent_pause', 'agent_resume', 'stop', 'agent_delete', 'estop', 'team_estop']);
const POP = { agent_pause: '💤', agent_resume: '▶️', stop: '⏹', agent_delete: '🗑' };
const KEY = 'ahub_office_dock';       // thu gọn / mở
const KEY_G = 'ahub_office_dock_g';   // nhóm đang thu gọn
const FILTERS = [['all', 'Tất cả'], ['need', 'Cần bạn'], ['working', 'Đang làm'], ['idle', 'Rảnh'], ['paused', 'Tạm dừng'], ['error', 'Lỗi']];
const SEARCH_MIN = 6; // ô tìm hiện khi có từ chừng này agent

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = String(text);
  return e;
}
function button(cls, text, onClick, label) {
  const b = el('button', cls, text);
  b.type = 'button';
  if (label) { b.setAttribute('aria-label', label); b.title = label; }
  b.addEventListener('click', onClick);
  return b;
}
function load(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (_) { return d; } }
function save(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) { /* chỉ là tiện lợi */ } }

// o: {root, api, statusOf(id) → khoá trạng thái hiển thị, select(id), openApproval(id), openQuestion(id), talk(id), chat(),
//     toast(msg, ok), refresh(), pop(agentId, emoji), onExpired(), can3d() → bảng 3D (duyệt/câu hỏi/nhắn) có vẽ được không}
export function createDock(o) {
  const box = el('aside', 'office-dock');
  box.id = 'office-dock';
  box.setAttribute('aria-label', 'Điều khiển agent');
  // ---- thanh tiêu đề
  const head = el('div', 'dock-head');
  const toggle = button('dock-toggle', '', () => setOpen(!open));
  toggle.setAttribute('aria-controls', 'office-dock-body');
  const tTitle = el('span', 'dock-title', 'Điều khiển agent');
  const tCount = el('span', 'dock-count', '');
  const tEstop = el('span', 'dock-estop-pill', 'ESTOP');
  const tNeed = el('span', 'dock-need', '');
  const tCaret = el('span', 'dock-caret', '▾');
  tCaret.setAttribute('aria-hidden', 'true');
  tEstop.hidden = tNeed.hidden = true;
  toggle.append(tTitle, tCount, tEstop, tNeed, tCaret);
  head.appendChild(toggle);
  // ---- thân
  const body = el('div', 'dock-body');
  body.id = 'office-dock-body';
  const banner = el('div', 'dock-banner');
  banner.hidden = true;
  const tools = el('div', 'dock-tools');
  const filters = el('div', 'dock-filters');
  filters.setAttribute('role', 'group');
  filters.setAttribute('aria-label', 'Lọc agent theo trạng thái');
  const search = el('input', 'dock-search');
  search.type = 'search'; search.placeholder = 'Tìm agent…'; search.setAttribute('aria-label', 'Tìm agent theo tên, id hoặc việc');
  search.addEventListener('input', () => { query = search.value; render(); });
  const estopBtn = button('dock-btn danger dock-estop', '⛔ ESTOP', () => estopChoice(null), 'Dừng khẩn cấp cả hub (ESTOP)');
  tools.append(filters, estopBtn);
  const list = el('div', 'dock-list');
  const panel = el('div', 'dock-confirm'); // hộp chọn / xác nhận (thay chỗ danh sách khi mở)
  panel.setAttribute('role', 'alertdialog');
  panel.setAttribute('aria-label', 'Xác nhận');
  panel.hidden = true;
  const live = el('p', 'sr-only');
  live.setAttribute('role', 'status');
  body.append(banner, tools, search, list, panel, live);
  box.append(head, body);
  o.root.appendChild(box);

  let open = load(KEY, null);
  if (open == null) open = !(window.matchMedia && window.matchMedia('(max-width: 640px)').matches);
  const collapsed = new Set(load(KEY_G, []));
  let blocked = false, busy = false, sig = '', state = null, focusId = null, panelOpen = null, filter = 'all', query = '', menuFor = null;

  function setOpen(v) { open = !!v; save(KEY, open); sync(); }
  function sync() {
    box.classList.toggle('closed', !open);
    box.classList.toggle('blocked', blocked);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    tCaret.textContent = open ? '▾' : '▸';
    body.hidden = !open || blocked;
    toggle.title = (open ? 'Thu gọn' : 'Mở') + ' bảng điều khiển agent';
  }
  // bảng 3D (duyệt / câu hỏi / chat / nhắn) đang mở → ẩn thân bảng cho khỏi che bảng đó; chỉ còn thanh tiêu đề
  function setBlocked(v) { if (blocked === !!v) return; blocked = !!v; sync(); }

  function errorOf(res) {
    if (res.status === 401) { o.onExpired(); return 'Phiên đăng nhập đã hết'; }
    return res.error || 'Lỗi';
  }
  function setBusy(v) {
    busy = v;
    box.classList.toggle('busy', v);
    for (const b of body.querySelectorAll('button')) if (!b.hasAttribute('data-hold')) b.disabled = v;
  }

  // ------------------------------------------------------------ hộp chọn / xác nhận
  function showPanel(kind, parts, focusEl, ctx) {
    if (!open) setOpen(true);
    panelOpen = Object.assign({ kind }, ctx || {});
    panel.replaceChildren(...parts);
    panel.hidden = false;
    list.hidden = tools.hidden = search.hidden = true;
    setTimeout(() => focusEl && focusEl.focus(), 0);
  }
  function closePanel(refocus) {
    const c = panelOpen;
    panelOpen = null;
    panel.hidden = true; panel.replaceChildren();
    list.hidden = tools.hidden = false;
    sig = ''; render();
    if (refocus && c && c.from) { const b = body.querySelector('[data-k="' + CSS.escape(c.from) + '"]'); if (b) b.focus(); }
  }
  function title(text, danger) { return el('p', 'dock-confirm-title' + (danger ? ' danger' : ''), text); }

  // ESTOP hub (team = null) / team: chọn mức trước (chỉ chặn run mới, hay dừng luôn run đang chạy) → rồi xác nhận 2 bước của server
  function estopChoice(team, from) {
    const name = team ? 'team_estop' : 'estop';
    const who = team ? 'team ' + (team.name || team.id) : 'cả hub';
    const fields = (stop) => Object.assign(team ? { id: team.id } : {}, { on: '1', stop_running: stop ? '1' : '0' });
    const a = button('dock-btn', 'Chỉ chặn run mới', () => control(name, fields(false), null, from), 'Chặn run mới, run đang chạy chạy nốt');
    const b = button('dock-btn danger', 'Chặn + dừng run đang chạy', () => control(name, fields(true), null, from), 'Chặn run mới và dừng mọi run đang chạy');
    const no = button('dock-btn', 'Huỷ', () => closePanel(true));
    const row = el('div', 'dock-confirm-btns col');
    row.append(a, b, no);
    showPanel('choice', [title('Dừng khẩn cấp ' + who, true),
      el('p', 'dock-confirm-msg', 'Agent không nhận run mới cho tới khi bạn cho chạy lại. Chọn mức:'), row], a, { from });
  }

  // tạm dừng / tiếp tục / dừng run / xoá agent / ESTOP — lần gửi đầu KHÔNG có confirm
  async function control(name, fieldsIn, agent, from) {
    if (!WRITE.has(name) || busy) return;
    const fields = typeof fieldsIn === 'string' ? { id: fieldsIn } : Object.assign({}, fieldsIn);
    setBusy(true);
    const res = await o.api.post('/office/do/' + encodeURIComponent(name), fields);
    setBusy(false);
    if (res.error) { o.toast(errorOf(res), false); if (panelOpen) closePanel(true); return; }
    const d = res.data || {};
    if (d.need === 'confirm') { askConfirm(name, fields, d, agent, from); return; }
    o.toast('Phản hồi không rõ từ hub', false); // server luôn hỏi xác nhận trước — không bao giờ "ok" ngay
  }
  function askConfirm(name, fields, d, agent, from) {
    const msg = el('p', 'dock-confirm-msg', d.message || 'Xác nhận thao tác này?');
    let note = null;
    const parts = [title('Xác nhận', d.danger), msg];
    if (d.note_label) {
      const lab = el('label', 'dock-note', d.note_label);
      note = el('textarea');
      note.rows = 2; note.maxLength = 2000;
      lab.appendChild(note);
      parts.push(lab);
    }
    const no = button('dock-btn', 'Huỷ', () => closePanel(true));
    const yes = button('dock-btn ' + (d.danger ? 'danger' : 'primary'), yesLabel(name, fields, d), async () => {
      // NGƯỜI bấm → mới gửi confirm=yes
      const extra = { confirm: 'yes' };
      if (note && note.value.trim()) extra.note = note.value.trim();
      await send(name, Object.assign({}, fields, extra), agent, from);
    });
    const row = el('div', 'dock-confirm-btns');
    row.append(no, yes);
    parts.push(row);
    showPanel('confirm', parts, note || yes, { from });
  }
  function yesLabel(name, fields, d) {
    if (name === 'stop') return 'Dừng run';
    if (name === 'agent_delete') return 'Xoá';
    if (name === 'estop' || name === 'team_estop') return fields.on === '1' ? 'Bật ESTOP' : 'Cho chạy lại';
    return d.danger ? 'Đồng ý' : 'Đồng ý';
  }
  // bước 2 (ESTOP / xoá agent): phải tích ô "Tôi hiểu…" rồi mới bấm được
  function askConfirm2(name, fields, d, agent, from) {
    const lab = el('label', 'dock-check');
    const cb = el('input');
    cb.type = 'checkbox';
    lab.append(cb, el('span', null, d.check || 'Tôi hiểu thao tác này'));
    const no = button('dock-btn', 'Huỷ', () => closePanel(true));
    const yes = button('dock-btn danger', 'Xác nhận lần 2', async () => {
      if (!cb.checked) return;
      await send(name, Object.assign({}, fields, { confirm2: 'yes' }), agent, from);
    });
    yes.disabled = true; yes.setAttribute('data-hold', '1');
    cb.addEventListener('change', () => { yes.disabled = !cb.checked || busy; });
    const row = el('div', 'dock-confirm-btns');
    row.append(no, yes);
    const parts = [title('Xác nhận lần 2', true)];
    if (d.message) parts.push(el('p', 'dock-confirm-msg muted', d.message));
    parts.push(lab, row);
    showPanel('confirm2', parts, cb, { from });
  }
  async function send(name, fields, agent, from) {
    setBusy(true);
    const res = await o.api.post('/office/do/' + encodeURIComponent(name), fields);
    setBusy(false);
    if (res.error) { o.toast(errorOf(res), false); return; }
    const r = res.data || {};
    if (r.need === 'confirm2' && r.kind === 'check') { askConfirm2(name, fields, r, agent, from); return; }
    if (!r.ok) { o.toast(r.need ? 'Thao tác cần thêm bước xác nhận — dùng giao diện 2D' : 'Phản hồi không rõ từ hub', false); closePanel(true); return; }
    o.toast(r.message || 'Đã xong', true);
    live.textContent = r.message || 'Đã xong';
    if (agent && POP[name]) o.pop(agent, POP[name]);
    closePanel(false);
    o.refresh();
  }
  box.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (panelOpen) { e.preventDefault(); e.stopPropagation(); closePanel(true); }
    else if (menuFor) { e.preventDefault(); e.stopPropagation(); const k = menuFor; menuFor = null; sig = ''; render(); const b = body.querySelector('[data-k="' + CSS.escape('menu:' + k) + '"]'); if (b) b.focus(); }
  });

  // ------------------------------------------------------------ vẽ
  function doAction(x, agentId, from) {
    menuFor = null;
    if (x.act === 'approval') o.openApproval(x.id);
    else if (x.act === 'question') o.openQuestion(x.id);
    else if (x.act === 'chat') o.chat();
    else if (x.act === 'talk') o.talk(x.id);
    else if (x.act === 'control') control(x.name, x.id, agentId, from);
  }
  function toggleGroup(key) {
    if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
    save(KEY_G, [...collapsed]);
    sig = ''; render();
    const b = body.querySelector('[data-k="' + CSS.escape('g:' + key) + '"]'); if (b) b.focus();
  }
  function dots(sum) {
    const w = el('span', 'dock-gsum');
    for (const [k, label] of [['need', 'cần bạn'], ['working', 'đang làm'], ['error', 'lỗi'], ['idle', 'rảnh'], ['paused', 'tạm dừng']]) {
      if (!sum[k]) continue;
      const s = el('span', 'dock-gdot st-' + k, sum[k]);
      s.title = sum[k] + ' ' + label;
      w.appendChild(s);
    }
    return w;
  }
  function render(s) {
    if (s) state = s;
    if (!state || panelOpen) return;
    const now = Date.now();
    const aps = state.approvals || [], qs = state.questions || [];
    const hubEstop = !!(state.hub && state.hub.estop);
    const teams = state.teams || [];
    const ags = (state.agents || []).map((a) => Object.assign({}, a, {
      status: o.statusOf(a.id, a) || a.status, is_manager: !!a.is_manager || a.id === state.manager_id,
    }));
    const all = rosterItems(ags, teams, state.manager_id);
    const shown = filterItems(all, filter, query);
    const sum = rosterSummary(ags);
    const can3d = o.can3d();
    const rows = shown.map((it) => {
      if (it.kind === 'group') {
        const t = it.team ? teams.find((x) => x.id === it.team) : null;
        return ['g', it.key, it.label, it.n, it.shown, JSON.stringify(it.sum), t ? [t.id, t.name || t.id, t.estop ? 1 : 0] : null];
      }
      if (collapsed.has(it.group) && !query) return null;
      const a = it.a;
      const ap = aps.find((x) => x.agent === a.id), q = qs.find((x) => x.agent === a.id);
      const acts = agentActions(a, ap, q).filter((x) => x.act === 'control' || can3d);
      const age = a.started_at ? fmtAge((now - Date.parse(a.started_at)) / 1000) : '';
      return ['a', a.id, a.name || a.id, a.status, a.team_role || '', a.task || '', a.tool || '', a.run_status || '', age,
        fmtUsd((Number(a.cost_today_usd) || 0) + (Number(a.run_cost_usd) || 0)), a.is_manager ? 1 : 0,
        acts.map((x) => [x.act, x.name || '', x.id, x.label, x.kind, x.sr, x.menu ? 1 : 0, x.detach ? 1 : 0])];
    }).filter(Boolean);
    // tiêu đề luôn cập nhật (kể cả khi thu gọn)
    tCount.textContent = String(ags.length);
    tCount.title = ags.length + ' agent';
    tNeed.hidden = !sum.need;
    tNeed.textContent = sum.need ? sum.need + ' cần bạn' : '';
    tEstop.hidden = !hubEstop;
    box.classList.toggle('estop', hubEstop);
    const nsig = JSON.stringify([rows, sum, focusId, can3d, filter, query, hubEstop, menuFor, [...collapsed], ags.length]);
    if (nsig === sig) return;
    sig = nsig;
    const ae = document.activeElement, fk = ae && box.contains(ae) ? ae.getAttribute('data-k') : null;
    // ESTOP toàn hub: dải đỏ + nút cho chạy lại (thay nút ⛔ ESTOP)
    banner.hidden = !hubEstop;
    estopBtn.hidden = hubEstop;
    if (hubEstop) {
      const off = button('dock-btn', '▶ Cho chạy lại', () => control('estop', { on: '0' }, null, 'estop-off'), 'Tắt ESTOP — cho mọi agent chạy lại');
      off.setAttribute('data-k', 'estop-off');
      banner.replaceChildren(el('span', null, '⛔ ESTOP đang bật — không run mới nào được chạy'), off);
    }
    // bộ lọc: chỉ hiện mục có agent (luôn có "Tất cả" + mục đang chọn)
    const counts = { all: ags.length, need: sum.need, working: sum.working, idle: sum.idle, paused: sum.paused, error: sum.error };
    filters.replaceChildren(...FILTERS.filter(([k]) => k === 'all' || counts[k] || k === filter).map(([k, label]) => {
      const b = button('dock-filter st-' + k + (filter === k ? ' on' : ''), label + ' ' + counts[k], () => { filter = filter === k ? 'all' : k; render(); });
      b.setAttribute('aria-pressed', filter === k ? 'true' : 'false');
      b.setAttribute('data-k', 'f:' + k);
      return b;
    }));
    search.hidden = ags.length < SEARCH_MIN && !query;
    const out = [];
    let ul = null;
    for (const r of rows) {
      if (r[0] === 'g') {
        const [, key, label, n, nShown, sumJ, team] = r;
        const sec = el('section', 'dock-group' + (team && team[2] ? ' estop' : ''));
        const gh = el('div', 'dock-ghead');
        const isOpen = !collapsed.has(key) || !!query;
        const gb = button('dock-gbtn', '', () => toggleGroup(key), (isOpen ? 'Thu gọn ' : 'Mở ') + label);
        gb.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
        gb.setAttribute('data-k', 'g:' + key);
        gb.append(el('span', 'dock-caret', isOpen ? '▾' : '▸'), el('span', 'dock-glabel', label), el('span', 'dock-gn', nShown === n ? n : nShown + '/' + n));
        gh.append(gb, dots(JSON.parse(sumJ)));
        if (team) {
          const [tid, tname, est] = team;
          const tb = est
            ? button('dock-btn go sm', '▶ Chạy lại', () => control('team_estop', { id: tid, on: '0' }, null, 'te:' + tid), 'Cho team ' + tname + ' chạy lại')
            : button('dock-btn ghost sm', '⛔ Dừng team', () => estopChoice({ id: tid, name: tname }, 'te:' + tid), 'Dừng khẩn cấp team ' + tname);
          tb.setAttribute('data-k', 'te:' + tid);
          gh.appendChild(tb);
        }
        sec.appendChild(gh);
        if (team && team[2]) sec.appendChild(el('p', 'dock-gnote', 'Team đang dừng khẩn — không nhận run mới'));
        ul = el('ul', 'dock-rows');
        sec.appendChild(ul);
        out.push(sec);
        continue;
      }
      const [, id, name, st, role, task, tool, runSt, age, cost, mgr, acts] = r;
      const li = el('li', 'dock-row st-' + st + (id === focusId ? ' focus' : ''));
      li.setAttribute('data-agent', id);
      const top = el('div', 'dock-top');
      const dot = el('span', 'dock-dot st-' + st);
      dot.setAttribute('aria-hidden', 'true');
      const nameBtn = button('dock-name', (mgr ? '★ ' : '') + name, () => o.select(id), 'Tới ' + name + ' trong văn phòng');
      nameBtn.setAttribute('data-k', 'sel:' + id);
      const sub = el('span', 'dock-sub', (STATUS_VI[st] || st) + (role ? ' · ' + (ROLE_VI[role] || role) : '') + (runSt === 'queued' ? ' · xếp hàng' : ''));
      top.append(dot, nameBtn, sub);
      const meta = el('div', 'dock-meta');
      meta.append(el('span', 'dock-task' + (task ? '' : ' none'), task || 'Chưa có việc'), el('span', 'dock-nums', [tool, age, cost].filter(Boolean).join(' · ')));
      const btns = el('div', 'dock-acts');
      const menuItems = [];
      for (const [act, nm, xid, label, kind, sr, inMenu, detach] of acts) {
        const k = act + ':' + (nm ? nm + ':' : '') + xid;
        const b = button('dock-btn ' + kind, label, () => doAction({ act, name: nm, id: detach ? { id: xid, detach_teams: '1' } : xid }, id, inMenu ? 'menu:' + id : k), sr);
        b.setAttribute('data-k', k);
        b.disabled = busy;
        if (inMenu) menuItems.push(b); else btns.appendChild(b);
      }
      if (menuItems.length) {
        const mb = button('dock-btn ghost dock-more', '⋯', () => { menuFor = menuFor === id ? null : id; sig = ''; render(); }, 'Thao tác khác cho ' + name);
        mb.setAttribute('aria-expanded', menuFor === id ? 'true' : 'false');
        mb.setAttribute('data-k', 'menu:' + id);
        btns.appendChild(mb);
      }
      li.append(top, meta, btns);
      if (menuFor === id && menuItems.length) {
        const m = el('div', 'dock-menu');
        m.append(...menuItems);
        li.appendChild(m);
      }
      (ul || list).appendChild(li);
    }
    if (!out.length) out.push(el('p', 'dock-empty', ags.length ? 'Không agent nào khớp bộ lọc.' : 'Chưa có agent nào.'));
    list.replaceChildren(...out);
    if (fk) { const b = box.querySelector('[data-k="' + CSS.escape(fk) + '"]'); if (b && !b.hidden) b.focus(); }
  }
  // chọn nhân vật trong cảnh → mở nhóm của nó, đánh dấu hàng và cuộn tới để thao tác ngay
  function focusAgent(id) {
    if (focusId === (id || null)) return;
    focusId = id || null;
    if (focusId && state) {
      const it = rosterItems(state.agents || [], state.teams || [], state.manager_id).find((x) => x.kind === 'agent' && x.a.id === focusId);
      if (it && collapsed.has(it.group)) { collapsed.delete(it.group); save(KEY_G, [...collapsed]); }
      if (filter !== 'all') filter = 'all';
    }
    render();
    if (focusId && open && !blocked) {
      const li = list.querySelector('[data-agent="' + CSS.escape(focusId) + '"]');
      if (li && li.scrollIntoView) li.scrollIntoView({ block: 'nearest' });
    }
  }
  // thời gian run đổi theo phút; trạng thái hiển thị đổi theo nhân vật — dựng lại nhẹ mỗi 5 s (chỉ khi chữ đổi)
  const tick = setInterval(() => render(), 5000);
  sync();
  return { el: box, render, control, setBlocked, focusAgent, isConfirming: () => !!panelOpen, dispose: () => clearInterval(tick) };
}
