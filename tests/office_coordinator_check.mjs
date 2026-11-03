// Kiểm KHÔNG cần trình duyệt (Node ≥ 18) cho "điều phối viên là nhân vật chính" (docs/dev-notes/office-coordinator.md): dựng World +
// Walker + Actor thật (canvas giả), mô phỏng thời gian. Avatar điều phối viên xuất hiện cạnh bàn trong phòng điều phối viên, tới được mọi
// phòng; ngồi bàn khoá di chuyển, đứng dậy ở chỗ trống; không bao giờ 2 điều phối viên hiện cùng lúc; agent rảnh ngồi bàn ≥ 80 % thời gian,
// nghỉ ngắn thưa (≤ 1 lần / 5 phút); chuyến server/thư viện thưa; agent đang nói chuyện đứng yên; talkPlan chọn đúng action.
// In "ok …" mỗi ca; lỗi → exit 1. Chạy: node tests/office_coordinator_check.mjs <thư mục gốc repo>   (tests/test_office_coordinator.py gọi)
const ctx = new Proxy({}, {
  get: (t, k) => (k === 'measureText' ? () => ({ width: 10 }) : k in t ? t[k] : () => ({ addColorStop() {} })),
  set: (t, k, v) => { t[k] = v; return true; },
});
globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => ctx, style: {}, addEventListener() {}, setAttribute() {}, appendChild() {} }) };
const root = process.argv[2].replace(/\\/g, '/');
const url = (p) => (root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/' + p;
const THREE = await import(url('vendor/three-0.169.0/three.module.min.js'));
const { World } = await import(url('office/world.js'));
const { Walker, blockedAt, deskSpawn, coordinatorOf, nearDesk, spawnPoint, WALK_R } = await import(url('office/walk.js'));
const { Actor, IDLE_DESK_MIN } = await import(url('office/actors.js'));
const { talkPlan } = await import(url('office/talk.js'));

let fails = 0;
const check = (name, cond) => { if (cond) console.log('ok ' + name); else { fails++; console.log('FAIL ' + name); } };

// ---- điểm xuất hiện + tới được mọi phòng + ngồi/đứng
for (const n of [1, 5, 19, 40]) for (const withTeams of [false, true]) {
  const teams = withTeams ? [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }] : [];
  const agents = [{ id: 'mgr', is_manager: true }];
  for (let i = 0; i < n; i++) agents.push({ id: 'ag' + i, team: withTeams ? (i % 2 ? 'a' : 'b') : null });
  const w = new World({ teams, agents, manager_id: 'mgr' }), g = w.grid, tag = `n=${n} teams=${withTeams}`;
  const d = deskSpawn(w), room = d && w.roomAt(d.x, d.z);
  const rooms = w.rooms.filter((r) => { const p = g.findPath(d.x, d.z, r.p.x, r.p.z), l = p[p.length - 1]; return l && Math.hypot(l.x - r.p.x, l.z - r.p.z) < 0.8; });
  check(`spawn ${tag}: cạnh bàn trong phòng điều phối viên, không bị chặn, tới được ${rooms.length}/${w.rooms.length} phòng`,
    d && room && room.key === 'mgr' && !blockedAt(g, d.x, d.z, WALK_R) && nearDesk(w, d) && rooms.length === w.rooms.length);
  if (n !== 5) continue;
  const scene = new THREE.Scene(), cam = new THREE.PerspectiveCamera(45, 1.6, 0.3, 400);
  const wk = new Walker({ camera: cam, scene });
  wk.setIdentity({ id: 'mgr', name: 'Điều phối viên', color: '#c8413a' });
  wk.enter(w);
  check(`walker ${tag}: vào chế độ đi bộ đứng ở deskSpawn`, wk.isCoordinator && Math.hypot(wk.pos.x - d.x, wk.pos.z - d.z) < 1e-6);
  wk.sit(w.mgrSpot);
  wk.key('KeyW', true, false);
  for (let i = 0; i < 30; i++) { wk.update(0.1, i * 0.1, false); wk.updateCamera(0.1, false); }
  check(`walker ${tag}: ngồi bàn → phím đi không di chuyển, chibi ở ghế`, wk.seated && wk.pos.x === w.mgrSpot.x && wk.pos.z === w.mgrSpot.z && wk.chibi.pose === 'type');
  wk.key('KeyW', false, false);
  wk.stand();
  check(`walker ${tag}: đứng dậy ở ô trống trong phòng`, !wk.seated && !blockedAt(g, wk.pos.x, wk.pos.z, WALK_R) && w.roomAt(wk.pos.x, wk.pos.z).key === 'mgr');
  wk.setIdentity(null); wk.exit(); wk.enter(w);
  const sp = spawnPoint(w);
  check(`walker ${tag}: không có điều phối viên → avatar chủ hub ở spawnPoint`, !wk.isCoordinator && wk.pos.x === sp.x && wk.pos.z === sp.z);
}

// ---- không có 2 điều phối viên + hành vi theo thời gian (19 agent, 2 team)
{
  const teams = [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }];
  const data = [{ id: 'mgr', is_manager: true, status: 'working', run_id: 'r_m' }];
  for (let i = 0; i < 19; i++) data.push({ id: 'ag' + i, team: i % 2 ? 'a' : 'b', status: 'idle' });
  const w = new World({ teams, agents: data, manager_id: 'mgr' });
  const scene = new THREE.Scene(), cam = new THREE.PerspectiveCamera(45, 1.6, 0.3, 400);
  const actors = new Map();
  for (const a of data) { const act = new Actor(a, '#888888'); act.attach(w); act.setData(a, '#888888', 0); scene.add(act.chibi.group); actors.set(a.id, act); }
  const wk = new Walker({ camera: cam, scene });
  wk.setIdentity({ id: 'mgr', name: 'Điều phối viên', color: '#c8413a' }); wk.enter(w);
  // mô phỏng vòng vẽ của office.js: puppet = actor điều phối viên khi đi bộ
  const stepAll = (dt, now) => {
    const coord = wk.active && wk.isCoordinator ? coordinatorOf(actors, 'mgr') : null;
    for (const a of actors.values()) { a.puppet = a === coord; a.update(dt, now, now, false, false); }
    if (wk.active) { wk.setStatus(coord ? coord.bubble : null); wk.update(dt, now, false); }
  };
  const visibleCoords = () => [...actors.values()].filter((a) => a.isManager && a.chibi.group.visible).length + (wk.active && wk.isCoordinator && wk.chibi.group.visible ? 1 : 0);
  const desk = new Map([...actors.keys()].map((id) => [id, 0])), breaks = new Map([...actors.keys()].map((id) => [id, 0]));
  const prevZone = new Map();
  const DT = 0.5, T = 2 * 3600;
  let maxCoords = 0, minCoords = 9;
  for (let now = 0; now < T; now += DT) {
    if (Math.abs(now - 1800) < 1e-9) wk.exit();          // tắt đi bộ giữa chừng → về như cũ
    if (Math.abs(now - 2400) < 1e-9) wk.enter(w);
    stepAll(DT, now);
    const c = visibleCoords(); maxCoords = Math.max(maxCoords, c); minCoords = Math.min(minCoords, c);
    for (const [id, a] of actors) {
      if (a.isManager) continue;
      if (a.zone === 'desk' && !a.path.length) desk.set(id, desk.get(id) + DT);
      const z = a.zone === 'desk' ? 'desk' : a.zone ? 'away' : '';
      if (prevZone.get(id) === 'desk' && z === 'away') breaks.set(id, breaks.get(id) + 1);
      prevZone.set(id, z);
    }
  }
  check(`không có 2 điều phối viên (đi bộ bật/tắt): luôn đúng 1 hiện (min ${minCoords}, max ${maxCoords})`, maxCoords === 1 && minCoords === 1);
  check('bong bóng trạng thái run điều phối viên hiện trên avatar', !!(actors.get('mgr').bubble) && wk.chibi.group.visible);
  const ids = [...actors.keys()].filter((id) => id !== 'mgr');
  const fr = ids.map((id) => desk.get(id) / T), minFr = Math.min(...fr), avgFr = fr.reduce((s, x) => s + x, 0) / fr.length;
  check(`agent rảnh ngồi bàn ≥ 80 % thời gian (min ${(minFr * 100).toFixed(1)} %, TB ${(avgFr * 100).toFixed(1)} %)`, minFr >= 0.8);
  const maxB = Math.max(...ids.map((id) => breaks.get(id))), totB = ids.reduce((s, id) => s + breaks.get(id), 0);
  check(`nghỉ ngắn thưa: ≤ 1 lần / ${IDLE_DESK_MIN / 60} phút mỗi agent (max ${maxB} lần / 2 giờ), vẫn có đi (tổng ${totB})`, maxB <= T / IDLE_DESK_MIN + 1 && totB >= ids.length);
  // agent làm việc: tool đọc/bash mỗi 10 s trong 30 phút → chuyến server/thư viện thưa
  const wa = actors.get('ag0');
  wa.setData({ id: 'ag0', team: 'b', status: 'working', run_id: 'r1', tool: 'Edit' }, '#888888', T);
  let trips = 0, inTrip = false;
  for (let now = T; now < T + 1800; now += DT) {
    const ph = Math.round((now - T) * 2) % 40; // mỗi 20 s: Read 8 s rồi Edit 12 s
    if (ph === 0) wa.event({ type: 'tool.start', tool: 'Read' }, now); else if (ph === 16) wa.event({ type: 'tool.start', tool: 'Edit' }, now);
    wa.update(DT, now, now, false, false);
    const t = !!wa.trip; if (t && !inTrip) trips++; inTrip = t;
  }
  check(`làm việc: chuyến server/thư viện thưa (${trips} chuyến / 30 phút; Read 8 s + Edit 12 s lặp lại — trước đây ~90)`, trips >= 5 && trips <= 20);
  // đang nói chuyện: đứng/ngồi yên tại chỗ dù tới lúc đi nghỉ
  const ta = actors.get('ag3');
  ta.wander = { zone: 'desk', until: 0, ph: 0 };
  for (let now = 0; now < 5; now += DT) ta.update(DT, T + 2000 + now, 0, false, false);
  const p0 = { x: ta.pos.x, z: ta.pos.z };
  ta.talking = { x: p0.x + 1, z: p0.z + 1 };
  for (let now = 0; now < 1200; now += DT) ta.update(DT, T + 2010 + now, 0, false, false);
  check('đang nói chuyện: agent không đi đâu trong 20 phút', Math.hypot(ta.pos.x - p0.x, ta.pos.z - p0.z) < 1e-6 && !ta.path.length);
}

// ---- action khi gửi tin
check('talkPlan: agent đang chạy → steer run hiện tại', talkPlan({ id: 'a', status: 'working', run_id: 'r1' }, { lastRun: 'r0' }, 'x').name === 'steer');
check('talkPlan: chờ duyệt / đang hỏi có run → steer', ['waiting', 'waiting_human', 'asking'].every((s) => talkPlan({ id: 'a', status: s, run_id: 'r1' }, null, 'x').name === 'steer'));
check('talkPlan: rảnh, có run của hội thoại → run_continue', JSON.stringify(talkPlan({ id: 'a', status: 'idle' }, { lastRun: 'r0' }, 'x')) === '{"name":"run_continue","fields":{"id":"r0","prompt":"x"}}');
check('talkPlan: rảnh, chưa có → run_start', JSON.stringify(talkPlan({ id: 'a', status: 'idle', run_id: null }, { lastRun: null }, 'x')) === '{"name":"run_start","fields":{"agent":"a","prompt":"x"}}');
process.exit(fails ? 1 : 0);
