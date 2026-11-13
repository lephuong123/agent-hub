// Kiểm "Điều khiển agent" KHÔNG cần trình duyệt (Node ≥ 18) — docs/dev-notes/office-agent-board.md:
// - hàm thuần của static/office/roster.js: nhóm (điều phối viên trước, team theo thứ tự team.list, không team cuối), thứ tự trong nhóm
//   (lead → cần bạn → tên), nút điều khiển + MỌI nút của một hàng (agentActions), fmtAge, tóm tắt;
// - bảng điều khiển là 2D (DOM, office/dock.js): World thật KHÔNG còn bảng quản lý agent trong cảnh.
// In một dòng mỗi ca; lỗi → exit 1. Chạy: node tests/office_roster_check.mjs <thư mục gốc repo>
const calls = [];
const ctx = new Proxy({}, {
  get: (t, k) => {
    if (k === 'measureText') return (s) => ({ width: String(s).length * 10 });
    if (k === 'fillText') return (s) => calls.push(String(s));
    return k in t ? t[k] : () => ({ addColorStop() {} });
  },
  set: (t, k, v) => { t[k] = v; return true; },
});
globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => ctx, style: {} }) };
const root = process.argv[2].replace(/\\/g, '/');
const url = (p) => (root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/office/' + p;
const R = await import(url('roster.js'));
const { World } = await import(url('world.js'));

let fails = 0;
const ok = (c, m) => { if (c) console.log('ok ' + m); else { fails++; console.log('FAIL ' + m); } };

// ---- nhóm + thứ tự
const teams = [{ id: 'b', name: 'Beta' }, { id: 'a', name: 'Alpha' }];
const agents = [
  { id: 'w1', name: 'Zed', team: 'a', team_role: 'worker', status: 'idle' },
  { id: 'w2', name: 'Amy', team: 'a', team_role: 'worker', status: 'waiting_human', approvals: 1 },
  { id: 'l1', name: 'Lead', team: 'a', team_role: 'lead', status: 'idle' },
  { id: 'mgr', name: 'Boss', is_manager: true, status: 'working' },
  { id: 'x', name: 'Loose', team: null, status: 'paused', paused: true },
  { id: 'y', name: 'Ghost', team: 'gone', status: 'idle' },
  { id: 'b1', name: 'Bob', team: 'b', team_role: 'worker', status: 'error' },
];
const items = R.rosterItems(agents, teams, 'mgr');
const labels = items.filter((i) => i.kind === 'group').map((i) => i.label);
ok(JSON.stringify(labels) === JSON.stringify(['Điều phối viên', 'Team Beta', 'Team Alpha', 'Không team']), 'nhóm: điều phối viên → team theo team.list → không team: ' + labels.join(' | '));
const order = items.filter((i) => i.kind === 'agent').map((i) => i.a.id);
ok(JSON.stringify(order) === JSON.stringify(['mgr', 'b1', 'l1', 'w2', 'w1', 'y', 'x']), 'thứ tự trong nhóm: lead → cần bạn → … → tạm dừng; cùng mức theo tên: ' + order.join(','));
ok(R.rosterItems([], [], null).length === 0, 'không agent → không mục');
ok(R.rosterItems([{ id: 'q', status: 'idle' }], [], null)[0].label === 'Agent', 'chỉ agent không team → nhóm "Agent"');

// ---- nút điều khiển
ok(JSON.stringify(R.rowControls({ id: 'a', paused: false, run_id: null }).map((c) => c.name)) === '["agent_pause"]', 'rảnh: chỉ Tạm dừng');
const rc = R.rowControls({ id: 'a', paused: true, run_id: 'r_1' });
ok(rc[0].name === 'agent_resume' && rc[0].id === 'a' && rc[1].name === 'stop' && rc[1].id === 'r_1', 'tạm dừng + có run: Tiếp tục (id agent) + Dừng run (id run)');

// ---- fmtAge theo phút
ok(R.fmtAge(5) === '<1 ph' && R.fmtAge(61) === '1 ph' && R.fmtAge(3725) === '1h02' && R.fmtAge(NaN) === '—', 'fmtAge');
// ---- tóm tắt
const s = R.rosterSummary(agents);
ok(s.working === 1 && s.need === 1 && s.error === 1 && s.paused === 1 && s.idle === 3, 'tóm tắt: ' + JSON.stringify(s));

// ---- mọi nút của một hàng (bảng 2D) — kể cả agent rảnh: Giao việc (run mới); xoá nằm trong menu ⋯
const acts = (a, ap, q) => R.agentActions(a, ap, q).map((x) => (x.menu ? '⋯' : '') + x.act + (x.name ? ':' + x.name : '') + '=' + x.id).join(',');
ok(acts({ id: 'w', name: 'W' }) === 'talk=w,control:agent_pause=w,⋯control:agent_delete=w', 'rảnh: Giao việc + Tạm dừng; Xoá trong menu');
ok(R.agentActions({ id: 'w' })[0].label.includes('Giao việc'), 'rảnh: nút đầu là "Giao việc"');
ok(acts({ id: 'w', paused: true, run_id: 'r_1' }, { id: 'ap_1' }, { id: 'q_1' }) === 'approval=ap_1,question=q_1,talk=w,control:agent_resume=w,control:stop=r_1,⋯control:agent_delete=w',
  'chờ duyệt + hỏi + tạm dừng + có run: Duyệt, Trả lời, Nhắn, Tiếp tục, Dừng run, ⋯Xoá');
ok(R.agentActions({ id: 'w', run_id: 'r' })[0].label.includes('Nhắn'), 'đang chạy: "Nhắn" (chen vào run)');
ok(acts({ id: 'm', is_manager: true }) === 'chat=m,control:agent_pause=m', 'điều phối viên: Chat, không có Xoá');
ok(R.agentActions({ id: 'w', name: '<b>x</b>', run_id: 'r' }).every((x) => x.sr && x.label), 'mọi nút có nhãn cho trình đọc màn hình');

// ---- nhóm mang id team + tóm tắt; lọc + tìm
const g = items.filter((i) => i.kind === 'group');
ok(g.map((x) => x.key).join(',') === 'mgr,t:b,t:a,loose' && g[2].team === 'a' && g[2].sum.need === 1, 'nhóm: key/team/tóm tắt theo nhóm');
ok(items.filter((i) => i.kind === 'agent').every((i) => g.some((x) => x.key === i.group)), 'mỗi agent biết nhóm của mình');
const f1 = R.filterItems(items, 'need', '');
ok(f1.filter((i) => i.kind === 'agent').map((i) => i.a.id).join(',') === 'w2' && f1[0].kind === 'group' && f1[0].shown === 1, 'lọc "cần bạn": chỉ w2, giữ tiêu đề nhóm của nó');
ok(R.filterItems(items, 'idle', '').filter((i) => i.kind === 'group').every((x) => x.shown > 0), 'lọc: không giữ nhóm rỗng');
ok(R.filterItems(items, 'all', 'zed').filter((i) => i.kind === 'agent').map((i) => i.a.id).join(',') === 'w1', 'tìm theo tên');
ok(R.filterItems(items, 'all', '').length === items.length, 'không lọc → giữ nguyên');

// ---- World thật: không còn bảng quản lý agent trong cảnh (điều khiển agent là bảng 2D góc màn hình)
const w = new World({ teams: [{ id: 'a' }], agents: [{ id: 'mgr', is_manager: true }, { id: 'x', team: 'a' }], manager_id: 'mgr' });
ok(!('agents' in w.boards) && w.focusables.length === 4 && Object.keys(w.boards).sort().join(',') === 'hud,kanban,needs,runs',
  'World: chỉ còn hud/kanban/needs/runs — ' + Object.keys(w.boards).join(','));
process.exit(fails ? 1 : 0);
