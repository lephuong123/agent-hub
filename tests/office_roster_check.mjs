// Kiểm "Bảng quản lý agent" KHÔNG cần trình duyệt (Node ≥ 18) — docs/dev-notes/office-agent-board.md:
// - hàm thuần của static/office/roster.js: nhóm (điều phối viên trước, team theo thứ tự team.list, không team cuối), thứ tự trong nhóm
//   (lead → cần bạn → tên), chia trang (không mất / lặp agent, tiêu đề nhóm không đứng một mình cuối trang), nút điều khiển, fmtAge;
// - vẽ bằng canvas giả: chỉ fillText (không HTML), vùng bấm hàng / nút / trang đúng loại, nút trang chỉ khi có trang kế;
// - vị trí trong World thật: giá vẽ ở mép nam phòng nghỉ, quay về camera, không chồng bảng "Cần bạn", camera nhìn thẳng không bị vật che.
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

// ---- chia trang
for (const n of [0, 1, 7, 8, 9, 25, 80]) {
  const ag = [{ id: 'm', is_manager: true, status: 'idle' }];
  for (let i = 0; i < n; i++) ag.push({ id: 'a' + i, team: ['t1', 't2', 't3', null][i % 4], status: 'idle' });
  const it = R.rosterItems(ag, [{ id: 't1' }, { id: 't2' }, { id: 't3' }], 'm');
  const pages = R.rosterPages(it);
  const seen = pages.flat().filter((i) => i.kind === 'agent').map((i) => i.a.id);
  const lonely = pages.some((p) => p.length && p[p.length - 1].kind === 'group');
  const headless = pages.some((p) => p.length && p[0].kind !== 'group');
  ok(seen.length === ag.length && new Set(seen).size === ag.length && !lonely && !headless,
    `trang n=${n}: ${pages.length} trang, đủ ${seen.length}/${ag.length} agent, không tiêu đề mồ côi, mỗi trang mở bằng tiêu đề nhóm`);
}

// ---- nút điều khiển
ok(JSON.stringify(R.rowControls({ id: 'a', paused: false, run_id: null }).map((c) => c.name)) === '["agent_pause"]', 'rảnh: chỉ Tạm dừng');
const rc = R.rowControls({ id: 'a', paused: true, run_id: 'r_1' });
ok(rc[0].name === 'agent_resume' && rc[0].id === 'a' && rc[1].name === 'stop' && rc[1].id === 'r_1', 'tạm dừng + có run: Tiếp tục (id agent) + Dừng run (id run)');

// ---- fmtAge theo phút
ok(R.fmtAge(5) === '<1 ph' && R.fmtAge(61) === '1 ph' && R.fmtAge(3725) === '1h02' && R.fmtAge(NaN) === '—', 'fmtAge');
// ---- tóm tắt
const s = R.rosterSummary(agents);
ok(s.working === 1 && s.need === 1 && s.error === 1 && s.paused === 1 && s.idle === 3, 'tóm tắt: ' + JSON.stringify(s));

// ---- vẽ: vùng bấm + chỉ fillText
const many = [{ id: 'm', is_manager: true, status: 'working', run_id: 'r_m', started_at: new Date(Date.now() - 125000).toISOString(), cost_today_usd: 0.5, run_cost_usd: 0.25 }];
for (let i = 0; i < 30; i++) many.push({ id: 'a' + i, name: '<b>tên ' + i + '</b>', team: 't', status: 'idle', task: 'việc ' + i, tool: 'Bash', approvals: i % 3, questions: i % 2 });
const board = { draw(sig, fn) { if (sig === this.sig) return; this.sig = sig; fn(ctx, R.ROSTER_W, R.ROSTER_H); }, sig: null };
calls.length = 0;
let r = R.drawRoster(board, { agents: many, teams: [{ id: 't', name: 'T' }], manager_id: 'm', nowMs: Date.now(), page: 0 });
const kinds = (k) => board.hits.filter((h) => h.kind === k);
ok(r.page === 0 && r.pages > 1, 'nhiều agent → nhiều trang (' + r.pages + ')');
ok(kinds('agent').length >= 8 && kinds('ctl').length >= kinds('agent').length, 'trang 1: hàng + nút (' + kinds('agent').length + ' hàng)');
ok(kinds('page').length === 1 && kinds('page')[0].dir === 1, 'trang 1: chỉ nút "Sau"');
ok(kinds('ctl').some((h) => h.name === 'stop' && h.id === 'r_m' && h.agent === 'm'), 'nút Dừng run mang id run + id agent');
ok(calls.some((t) => t.includes('<b>tên')), 'tên agent vẽ nguyên văn bằng fillText (là DỮ LIỆU, không HTML)');
ok(calls.includes('2 ph') && calls.includes('$0.75'), 'thời gian run (phút) + chi phí hôm nay = đã xong + run đang chạy');
r = R.drawRoster(board, { agents: many, teams: [], manager_id: 'm', nowMs: Date.now(), page: 99 });
ok(r.page === r.pages - 1 && kinds('page').length === 1 && kinds('page')[0].dir === -1, 'trang vượt → trang cuối, chỉ nút "Trước"');
for (const h of board.hits) if (h.x < 0 || h.y < 0 || h.x + h.w > R.ROSTER_W || h.y + h.h > R.ROSTER_H) { ok(false, 'vùng bấm ra ngoài canvas ' + JSON.stringify(h)); break; }

// ---- vị trí trong World thật: giá vẽ ở mép nam phòng nghỉ, quay về camera, không chồng bảng "Cần bạn"; camera nhìn thẳng (như
// flyToFrame) thấy được MỌI điểm của mặt bảng — không vật nào che (lỗi cũ: treo tường tây dải cafe → 2 máy arcade che nửa dưới)
const THREE = await import(url('../vendor/three-0.169.0/three.module.min.js'));
for (const n of [1, 5, 19, 40, 80]) {
  const ag = [{ id: 'mgr', is_manager: true }];
  for (let i = 0; i < n; i++) ag.push({ id: 'ag' + i, team: i % 2 ? 'a' : 'b' });
  const w = new World({ teams: [{ id: 'a' }, { id: 'b' }], agents: ag, manager_id: 'mgr' });
  w.group.updateMatrixWorld(true);
  const b = w.boards.agents, f = b.frame(), room = w.roomAt(f.center.x, f.center.z - 0.3);
  const nf = w.boards.needs.frame();
  const apart = Math.abs(f.center.x - nf.center.x) > (b.w + w.boards.needs.w) / 2;
  const lounge = w.rooms.find((r) => r.key === 'lounge');
  const inside = f.center.x - b.w / 2 >= lounge.x0 && f.center.x + b.w / 2 <= lounge.x1;
  const dist = (b.h / 2) / Math.tan((50 * Math.PI) / 360) * 1.32;
  const eye = f.center.clone().addScaledVector(f.normal, dist).add(new THREE.Vector3(0, dist * 0.22, 0));
  const ray = new THREE.Raycaster();
  let blocked = 0;
  const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), f.normal).normalize();
  const up = new THREE.Vector3().crossVectors(f.normal, right).normalize();
  for (const u of [-0.45, 0, 0.45]) for (const v of [-0.45, 0, 0.45]) {
    const p = f.center.clone().addScaledVector(right, u * b.w).addScaledVector(up, v * b.h);
    const dir = p.clone().sub(eye); const len = dir.length(); dir.normalize();
    ray.set(eye, dir); ray.far = len - 0.02;
    if (ray.intersectObject(w.group, true).some((h) => h.object !== b.mesh && h.object.visible !== false)) blocked++;
  }
  ok(room && room.key === 'lounge' && inside && apart && f.normal.z > 0.9 && f.center.y + b.h / 2 < 2.8 && w.focusables.includes(b.mesh) && !blocked,
    `vị trí n=${n}: phòng nghỉ x ${(f.center.x - b.w / 2).toFixed(1)}…${(f.center.x + b.w / 2).toFixed(1)}, quay nam, không che (${blocked}/9 tia bị chặn)`);
}
process.exit(fails ? 1 : 0);
