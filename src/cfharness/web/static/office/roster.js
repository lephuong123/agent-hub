/* Văn phòng 3D — "Bảng quản lý agent" (hợp đồng + nguồn ý tưởng: docs/dev-notes/office-agent-board.md).
 * Bảng treo tường tây (dải cafe) liệt kê MỌI agent: nhóm theo team (điều phối viên trước), trong nhóm: lead trước, rồi agent cần bạn
 * (chờ duyệt / hỏi / lỗi) lên đầu. Mỗi hàng: đèn trạng thái (STATUS_COLOR), tên + vai trò team, việc đang làm, tool cuối, thời gian run,
 * chi phí hôm nay, huy hiệu duyệt/câu hỏi, nút Tạm dừng|Tiếp tục và Dừng run. Nhiều agent → chia trang (nút ◀ ▶).
 * Bấm hàng → camera bay tới nhân vật + thẻ (như bấm nhân vật). Nút ghi đi qua flows.control → POST /office/do/<name> (xác nhận 3D).
 * Chữ của agent (tên, việc, tool) là DỮ LIỆU đã cắt ở server — ở đây chỉ fillText (cắt thêm theo bề rộng). Hàm thuần ở đầu file
 * (rosterItems, rosterPages, fmtAge, rosterSummary) được kiểm bằng tests/office_roster_check.mjs. */
import { FONT, fitText, roundRect } from './util.js';
import { STATUS_COLOR, fmtUsd } from './boards.js';

export const ROSTER_W = 1400, ROSTER_H = 860;
const TOP = 196, ROW_H = 62, GROUP_H = 40, FOOT = 74;
// thứ tự "cần chú ý" trong một nhóm (ý từ agent-dashboard / claw-fleet: nhóm "Needs you" lên đầu)
const ATTN = { waiting_human: 0, asking: 1, error: 2, waiting: 3, working: 4, queued: 5, idle: 6, paused: 7 };
const ROLE_ORDER = { lead: 0, reviewer: 1, verifier: 2, worker: 3 };
const ROLE_VI = { lead: 'trưởng nhóm', worker: 'thành viên', reviewer: 'review', verifier: 'kiểm tra' };
export const SUMMARY = [['working', 'đang làm'], ['need', 'cần bạn'], ['error', 'lỗi'], ['idle', 'rảnh'], ['paused', 'tạm dừng']];

// Thời gian run (giây) → chữ ngắn đổi theo PHÚT (bảng chỉ vẽ lại khi chữ đổi — không upload texture mỗi giây).
export function fmtAge(s) {
  if (!(s >= 0)) return '—';
  if (s < 60) return '<1 ph';
  const m = Math.floor(s / 60);
  return m < 60 ? m + ' ph' : Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0');
}

// agents: [{id, name, team, team_role, status, is_manager, …}], teams: [{id, name}] (thứ tự team.list), managerId
// → mục hiển thị: {kind:'group', label, n} | {kind:'agent', a}. Điều phối viên một nhóm riêng ở đầu; team theo thứ tự team.list;
// agent không team ở cuối. Trong nhóm: lead → cần chú ý → tên.
export function rosterItems(agents, teams, managerId) {
  const list = (agents || []).filter((a) => a && a.id);
  const isMgr = (a) => !!a.is_manager || (managerId && a.id === managerId);
  const groups = [];
  const mgr = list.filter(isMgr);
  if (mgr.length) groups.push({ label: 'Điều phối viên', rows: mgr });
  const rest = list.filter((a) => !isMgr(a));
  const known = new Set();
  for (const t of teams || []) {
    if (!t || !t.id) continue;
    known.add(t.id);
    const rows = rest.filter((a) => a.team === t.id);
    if (rows.length) groups.push({ label: 'Team ' + (t.name || t.id), rows });
  }
  const loose = rest.filter((a) => !a.team || !known.has(a.team));
  if (loose.length) groups.push({ label: groups.length ? 'Không team' : 'Agent', rows: loose });
  const key = (a) => [ROLE_ORDER[a.team_role] ?? 9, ATTN[a.status] ?? 9, String(a.name || a.id).toLowerCase()];
  const cmp = (x, y) => { const a = key(x), b = key(y); return a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0); };
  const out = [];
  for (const g of groups) {
    out.push({ kind: 'group', label: g.label, n: g.rows.length });
    for (const a of g.rows.slice().sort(cmp)) out.push({ kind: 'agent', a });
  }
  return out;
}

// Chia trang theo chiều cao (px canvas). Tiêu đề nhóm không đứng một mình cuối trang; nhóm sang trang mới → lặp tiêu đề "(tiếp)".
export function rosterPages(items, avail) {
  const H = avail || ROSTER_H - TOP - FOOT;
  const pages = [];
  let cur = [], used = 0, group = null;
  const flush = () => { if (cur.length) pages.push(cur); cur = []; used = 0; };
  for (let i = 0; i < items.length; i++) {
    const it = items[i];
    if (it.kind === 'group') {
      group = it;
      if (used + GROUP_H + ROW_H > H) flush(); // tiêu đề + ít nhất một hàng
      cur.push(it); used += GROUP_H;
      continue;
    }
    if (used + ROW_H > H) {
      flush();
      if (group) { cur.push({ kind: 'group', label: group.label + ' (tiếp)', n: group.n }); used += GROUP_H; }
    }
    cur.push(it); used += ROW_H;
  }
  flush();
  return pages.length ? pages : [[]];
}

// Đếm cho dải tóm tắt đầu bảng.
export function rosterSummary(agents) {
  const c = { working: 0, need: 0, error: 0, idle: 0, paused: 0 };
  for (const a of agents || []) {
    const s = a.status;
    if (s === 'waiting_human' || s === 'asking' || s === 'waiting' || a.approvals || a.questions) c.need++;
    if (s === 'working' || s === 'queued') c.working++;
    else if (s === 'error') c.error++;
    else if (s === 'paused') c.paused++;
    else if (s === 'idle') c.idle++;
  }
  return c;
}

const CTL_W = 128, CTL_H = 42;
// nút điều khiển của một hàng (px canvas) — CHỈ mô tả; ghi thật đi qua flows.control (xác nhận do server soạn)
export function rowControls(a) {
  const out = [{ name: a.paused ? 'agent_resume' : 'agent_pause', label: a.paused ? '▶ Tiếp tục' : '⏸ Tạm dừng', id: a.id, kind: a.paused ? 'go' : 'pause' }];
  if (a.run_id) out.push({ name: 'stop', label: '■ Dừng run', id: a.run_id, kind: 'danger' });
  return out;
}

/* Vẽ bảng. d = {agents (đã gắn status hiển thị), teams, manager_id, nowMs, page, names}; trả {page, pages}. board.hits = vùng bấm:
 *   {kind:'agent', id} | {kind:'ctl', name, id, agent} | {kind:'page', dir}. */
export function drawRoster(board, d) {
  const items = rosterItems(d.agents, d.teams, d.manager_id);
  const pages = rosterPages(items);
  const page = Math.max(0, Math.min(pages.length - 1, d.page | 0));
  const nAgents = (d.agents || []).length;
  const sum = rosterSummary(d.agents);
  const rows = pages[page].map((it) => it.kind === 'group' ? ['g', it.label, it.n] : ['a', it.a.id, it.a.name, it.a.status, it.a.team_role, it.a.task,
    it.a.tool, it.a.paused, it.a.run_id, it.a.run_status, fmtAge(it.a.started_at ? (d.nowMs - Date.parse(it.a.started_at)) / 1000 : NaN),
    fmtUsd((Number(it.a.cost_today_usd) || 0) + (Number(it.a.run_cost_usd) || 0)), it.a.approvals || 0, it.a.questions || 0, it.a.is_manager]);
  const sig = JSON.stringify([rows, page, pages.length, sum, nAgents, d.statusVi ? 1 : 0]);
  board.draw(sig, (ctx, W, H) => {
    board.hits = [];
    ctx.fillStyle = '#f6f3ec'; ctx.fillRect(0, 0, W, H);
    // tiêu đề
    ctx.fillStyle = '#263041'; ctx.fillRect(0, 0, W, 86);
    ctx.textBaseline = 'middle'; ctx.fillStyle = '#ffffff'; ctx.font = '700 44px ' + FONT;
    ctx.fillText('Bảng quản lý agent', 32, 45);
    ctx.font = '600 28px ' + FONT; ctx.textAlign = 'right'; ctx.fillStyle = '#c9d3e6';
    ctx.fillText(nAgents + ' agent', W - 32, 46); ctx.textAlign = 'left';
    // dải tóm tắt (đèn + số) — ý từ agent farm: tổng quan trạng thái của cả đội trước khi đọc từng hàng
    let x = 32;
    const chip = (col, label) => {
      ctx.font = '600 24px ' + FONT;
      const w = ctx.measureText(label).width + 52;
      ctx.fillStyle = '#ffffff'; roundRect(ctx, x, 102, w, 44, 22); ctx.fill();
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x + 22, 124, 9, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#2d2a24'; ctx.fillText(label, x + 38, 125);
      x += w + 12;
    };
    chip(STATUS_COLOR.working, sum.working + ' đang làm');
    chip(STATUS_COLOR.waiting_human, sum.need + ' cần bạn');
    chip(STATUS_COLOR.error, sum.error + ' lỗi');
    chip(STATUS_COLOR.idle, sum.idle + ' rảnh');
    chip('#9aa3b5', sum.paused + ' tạm dừng');
    // tiêu đề cột
    const C = { name: 54, task: 340, tool: 668, age: 800, cost: 890, badge: 1000, ctl: 1100 };
    ctx.fillStyle = '#8a857a'; ctx.font = '700 19px ' + FONT;
    ctx.fillText('AGENT', C.name, 176); ctx.fillText('VIỆC ĐANG LÀM', C.task, 176); ctx.fillText('TOOL', C.tool, 176);
    ctx.fillText('RUN', C.age, 176); ctx.fillText('HÔM NAY', C.cost, 176); ctx.fillText('CẦN BẠN', C.badge, 176); ctx.fillText('ĐIỀU KHIỂN', C.ctl, 176);
    if (!nAgents) { ctx.fillStyle = '#8a857a'; ctx.font = '600 30px ' + FONT; ctx.fillText('Chưa có agent nào.', 32, TOP + 40); }
    let y = TOP;
    let alt = 0;
    for (const it of pages[page]) {
      if (it.kind === 'group') {
        ctx.fillStyle = '#5a5649'; ctx.font = '800 21px ' + FONT;
        ctx.fillText(fitText(ctx, it.label.toUpperCase() + ' · ' + it.n, W - 64), 32, y + GROUP_H / 2 + 2);
        ctx.fillStyle = '#d9d2c3'; ctx.fillRect(32, y + GROUP_H - 4, W - 64, 2);
        y += GROUP_H; alt = 0;
        continue;
      }
      const a = it.a, st = a.status || 'idle', col = STATUS_COLOR[st] || '#73726c';
      const hot = st === 'waiting_human' || st === 'asking' || st === 'error';
      ctx.fillStyle = hot ? '#fbe9e4' : alt++ % 2 ? '#efebe2' : '#fbf9f4';
      roundRect(ctx, 20, y + 3, W - 40, ROW_H - 6, 12); ctx.fill();
      board.hits.push({ x: 20, y: y + 3, w: C.ctl - 30, h: ROW_H - 6, kind: 'agent', id: a.id });
      // đèn trạng thái (vòng sáng nhẹ khi đang làm / cần bạn)
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(36, y + ROW_H / 2, 10, 0, Math.PI * 2); ctx.fill();
      if (st === 'working' || hot) { ctx.strokeStyle = col; ctx.globalAlpha = 0.35; ctx.lineWidth = 5; ctx.beginPath(); ctx.arc(36, y + ROW_H / 2, 15, 0, Math.PI * 2); ctx.stroke(); ctx.globalAlpha = 1; }
      ctx.fillStyle = '#1f1e1d'; ctx.font = '700 25px ' + FONT;
      ctx.fillText(fitText(ctx, (a.is_manager ? '★ ' : '') + (a.name || a.id), C.task - C.name - 16), C.name, y + 21);
      ctx.fillStyle = col; ctx.font = '600 18px ' + FONT;
      const sub = ((d.statusVi && d.statusVi[st]) || st) + (a.team_role ? ' · ' + (ROLE_VI[a.team_role] || a.team_role) : '') + (a.run_status === 'queued' ? ' · xếp hàng' : '');
      ctx.fillText(fitText(ctx, sub, C.task - C.name - 16), C.name, y + 45);
      ctx.fillStyle = a.task ? '#2d2a24' : '#a3a196'; ctx.font = '500 22px ' + FONT;
      ctx.fillText(fitText(ctx, a.task || '—', C.tool - C.task - 18), C.task, y + ROW_H / 2);
      ctx.fillStyle = '#5a5649'; ctx.font = '500 20px ui-monospace, Menlo, monospace';
      ctx.fillText(fitText(ctx, a.tool || '—', C.age - C.tool - 14), C.tool, y + ROW_H / 2);
      const age = a.started_at ? fmtAge((d.nowMs - Date.parse(a.started_at)) / 1000) : '—';
      ctx.fillStyle = a.started_at ? '#2f6b3d' : '#a3a196'; ctx.font = '700 21px ui-monospace, Menlo, monospace';
      ctx.fillText(age, C.age, y + ROW_H / 2);
      ctx.fillStyle = '#8a6514'; ctx.fillText(fmtUsd((Number(a.cost_today_usd) || 0) + (Number(a.run_cost_usd) || 0)), C.cost, y + ROW_H / 2);
      let bx = C.badge;
      const badge = (txt, bg) => {
        ctx.font = '700 19px ' + FONT;
        const w = ctx.measureText(txt).width + 18;
        ctx.fillStyle = bg; roundRect(ctx, bx, y + ROW_H / 2 - 15, w, 30, 15); ctx.fill();
        ctx.fillStyle = '#ffffff'; ctx.fillText(txt, bx + 9, y + ROW_H / 2 + 1);
        bx += w + 6;
      };
      if (a.approvals) badge('🛡' + a.approvals, '#c4483a');
      if (a.questions) badge('?' + a.questions, '#7b5ea7');
      if (!a.approvals && !a.questions) { ctx.fillStyle = '#c4beb1'; ctx.font = '500 20px ' + FONT; ctx.fillText('—', C.badge, y + ROW_H / 2); }
      // nút
      rowControls(a).forEach((c, i) => {
        const cx = C.ctl + i * (CTL_W + 8), cy = y + (ROW_H - CTL_H) / 2;
        ctx.fillStyle = c.kind === 'danger' ? '#c4483a' : c.kind === 'go' ? '#4d7c43' : '#6b6a63';
        roundRect(ctx, cx, cy, CTL_W, CTL_H, 12); ctx.fill();
        ctx.fillStyle = '#ffffff'; ctx.font = '700 18px ' + FONT; ctx.textAlign = 'center';
        ctx.fillText(fitText(ctx, c.label, CTL_W - 10), cx + CTL_W / 2, cy + CTL_H / 2 + 1); ctx.textAlign = 'left';
        board.hits.push({ x: cx, y: cy, w: CTL_W, h: CTL_H, kind: 'ctl', name: c.name, id: c.id, agent: a.id });
      });
      y += ROW_H;
    }
    // chân bảng: phân trang
    const fy = H - FOOT + 12;
    ctx.fillStyle = '#e8e2d5'; ctx.fillRect(0, H - FOOT, W, FOOT);
    ctx.fillStyle = '#5a5649'; ctx.font = '600 24px ' + FONT; ctx.textAlign = 'center';
    ctx.fillText('Trang ' + (page + 1) + '/' + pages.length + ' · bấm một hàng để tới nhân vật', W / 2, fy + 25); ctx.textAlign = 'left';
    const pbtn = (px, label, dir, on) => {
      ctx.fillStyle = on ? '#263041' : '#c9c3b6'; roundRect(ctx, px, fy, 150, 50, 14); ctx.fill();
      ctx.fillStyle = '#ffffff'; ctx.font = '700 22px ' + FONT; ctx.textAlign = 'center'; ctx.fillText(label, px + 75, fy + 26); ctx.textAlign = 'left';
      if (on) board.hits.push({ x: px, y: fy, w: 150, h: 50, kind: 'page', dir });
    };
    pbtn(24, '◀ Trước', -1, page > 0);
    pbtn(W - 174, 'Sau ▶', 1, page < pages.length - 1);
  });
  return { page, pages: pages.length };
}
