/* Văn phòng 3D — hàm THUẦN của "Điều khiển agent" (hợp đồng + nguồn ý tưởng: docs/dev-notes/office-agent-board.md).
 * Bảng điều khiển là 2D (DOM, góc màn hình — office/dock.js); file này chỉ tính: nhóm theo team (điều phối viên trước), trong nhóm
 * lead trước rồi agent cần bạn (chờ duyệt / hỏi / lỗi) lên đầu; đếm tóm tắt; nút điều khiển của một hàng; thời gian run.
 * Không DOM, không mạng — kiểm bằng tests/office_roster_check.mjs. */

// thứ tự "cần chú ý" trong một nhóm (ý từ agent-dashboard / claw-fleet: nhóm "Needs you" lên đầu)
const ATTN = { waiting_human: 0, asking: 1, error: 2, waiting: 3, working: 4, queued: 5, idle: 6, paused: 7 };
const ROLE_ORDER = { lead: 0, reviewer: 1, verifier: 2, worker: 3 };
export const ROLE_VI = { lead: 'trưởng nhóm', worker: 'thành viên', reviewer: 'review', verifier: 'kiểm tra' };
export const SUMMARY = [['working', 'đang làm'], ['need', 'cần bạn'], ['error', 'lỗi'], ['idle', 'rảnh'], ['paused', 'tạm dừng']];

// Thời gian run (giây) → chữ ngắn đổi theo PHÚT (bảng chỉ dựng lại khi chữ đổi).
export function fmtAge(s) {
  if (!(s >= 0)) return '—';
  if (s < 60) return '<1 ph';
  const m = Math.floor(s / 60);
  return m < 60 ? m + ' ph' : Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0');
}

// agents: [{id, name, team, team_role, status, is_manager, …}], teams: [{id, name}] (thứ tự team.list), managerId
// → mục hiển thị: {kind:'group', key, label, n, team (id team | null)} | {kind:'agent', a, group (key nhóm)}. Điều phối viên một nhóm riêng ở đầu; team theo thứ tự team.list;
// agent không team ở cuối. Trong nhóm: lead → cần chú ý → tên.
export function rosterItems(agents, teams, managerId) {
  const list = (agents || []).filter((a) => a && a.id);
  const isMgr = (a) => !!a.is_manager || (managerId && a.id === managerId);
  const groups = [];
  const mgr = list.filter(isMgr);
  if (mgr.length) groups.push({ key: 'mgr', team: null, label: 'Điều phối viên', rows: mgr });
  const rest = list.filter((a) => !isMgr(a));
  const known = new Set();
  for (const t of teams || []) {
    if (!t || !t.id) continue;
    known.add(t.id);
    const rows = rest.filter((a) => a.team === t.id);
    if (rows.length) groups.push({ key: 't:' + t.id, team: t.id, estop: !!t.estop, label: 'Team ' + (t.name || t.id), rows });
  }
  const loose = rest.filter((a) => !a.team || !known.has(a.team));
  if (loose.length) groups.push({ key: 'loose', team: null, label: groups.length ? 'Không team' : 'Agent', rows: loose });
  const key = (a) => [ROLE_ORDER[a.team_role] ?? 9, ATTN[a.status] ?? 9, String(a.name || a.id).toLowerCase()];
  const cmp = (x, y) => { const a = key(x), b = key(y); return a[0] - b[0] || a[1] - b[1] || (a[2] < b[2] ? -1 : a[2] > b[2] ? 1 : 0); };
  const out = [];
  for (const g of groups) {
    out.push({ kind: 'group', key: g.key, team: g.team, estop: !!g.estop, label: g.label, n: g.rows.length, sum: rosterSummary(g.rows) });
    for (const a of g.rows.slice().sort(cmp)) out.push({ kind: 'agent', a, group: g.key });
  }
  return out;
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

// nút điều khiển của một hàng — CHỈ mô tả; ghi thật đi qua dock.control → POST /office/do (xác nhận do server soạn)
export function rowControls(a) {
  const out = [{ name: a.paused ? 'agent_resume' : 'agent_pause', label: a.paused ? '▶ Tiếp tục' : '⏸ Tạm dừng', id: a.id, kind: a.paused ? 'go' : 'pause' }];
  if (a.run_id) out.push({ name: 'stop', label: '■ Dừng run', id: a.run_id, kind: 'danger' });
  return out;
}

// MỌI nút của một hàng trên bảng điều khiển 2D, theo thứ tự hiển thị. `ap` / `q` = yêu cầu duyệt / câu hỏi đang chờ đầu tiên của agent.
// act: approval (mở bảng duyệt — đọc đủ lệnh trước khi cho phép) · question · chat (điều phối viên) / talk (agent đang chạy: nhắn chen vào
// run; agent rảnh: "Giao việc" = run mới — luôn hỏi xác nhận) · control (tạm dừng / tiếp tục / dừng run / xoá — server soạn xác nhận).
// `menu: true` = nằm trong menu ⋯ (thao tác hiếm, nguy hiểm — tránh bấm nhầm).
export function agentActions(a, ap, q) {
  const nm = a.name || a.id;
  const out = [];
  if (ap) out.push({ act: 'approval', id: ap.id, label: '🛡 Duyệt', kind: 'danger', sr: 'Mở yêu cầu duyệt của ' + nm });
  if (q) out.push({ act: 'question', id: q.id, label: '❓ Trả lời', kind: 'ask', sr: 'Trả lời câu hỏi của ' + nm });
  if (a.is_manager) out.push({ act: 'chat', id: a.id, label: '💬 Chat', kind: 'ghost', sr: 'Chat với điều phối viên ' + nm });
  else if (a.run_id) out.push({ act: 'talk', id: a.id, label: '💬 Nhắn', kind: 'ghost', sr: 'Nhắn vào run đang chạy của ' + nm });
  else out.push({ act: 'talk', id: a.id, label: '▶ Giao việc', kind: a.paused ? 'ghost' : 'go', sr: 'Giao việc mới cho ' + nm + ' (bắt đầu run mới)' });
  for (const c of rowControls(a)) {
    out.push({ act: 'control', name: c.name, id: c.id, label: c.label, kind: c.kind,
      sr: c.name === 'stop' ? 'Dừng run đang chạy của ' + nm : c.name === 'agent_resume' ? 'Cho ' + nm + ' hoạt động lại' : 'Tạm dừng ' + nm });
  }
  if (!a.is_manager) out.push({ act: 'control', name: 'agent_delete', id: a.id, label: '🗑 Xoá agent…', kind: 'danger', menu: true, detach: !!a.team,
    sr: 'Xoá agent ' + nm + ' (vào thùng rác' + (a.team ? ', gỡ khỏi team' : '') + ')' });
  return out;
}

// Lọc theo dải tóm tắt (all | need | working | idle | paused | error) + tìm theo tên/id/việc. Giữ tiêu đề nhóm còn hàng; số đếm nhóm theo kết quả lọc.
export function filterItems(items, filter, query) {
  const q = String(query || '').trim().toLowerCase();
  const need = (a) => a.status === 'waiting_human' || a.status === 'asking' || a.status === 'waiting' || !!a.approvals || !!a.questions;
  const keep = (a) => {
    if (filter === 'need' && !need(a)) return false;
    if (filter === 'working' && !(a.status === 'working' || a.status === 'queued')) return false;
    if (['idle', 'paused', 'error'].includes(filter) && a.status !== filter) return false;
    if (q && !(String(a.name || '') + ' ' + a.id + ' ' + String(a.task || '')).toLowerCase().includes(q)) return false;
    return true;
  };
  const out = [];
  let g = null, rows = [];
  const flush = () => { if (g && rows.length) out.push(Object.assign({}, g, { shown: rows.length }), ...rows); rows = []; };
  for (const it of items) {
    if (it.kind === 'group') { flush(); g = it; continue; }
    if (keep(it.a)) rows.push(it);
  }
  flush();
  return out;
}
