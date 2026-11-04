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
const { Walker, blockedAt, deskSpawn, doorSpawn, coordinatorOf, nearDesk, spawnPoint, WALK_R } = await import(url('office/walk.js'));
const { Actor, IDLE_DESK_MIN } = await import(url('office/actors.js'));
const T = await import(url('office/talk.js'));
const { talkPlan } = T;

let fails = 0;
const check = (name, cond) => { if (cond) console.log('ok ' + name); else { fails++; console.log('FAIL ' + name); } };

// ---- điểm xuất hiện + tới được mọi phòng + ngồi/đứng
const mkWorld = (n, nt) => {
  const teams = Array.from({ length: nt }, (_, i) => ({ id: 't' + i, name: 'T' + i }));
  const agents = [{ id: 'mgr', is_manager: true }];
  for (let i = 0; i < n; i++) agents.push({ id: 'ag' + i, team: nt ? 't' + (i % nt) : null });
  return new World({ teams, agents, manager_id: 'mgr' });
};
for (const n of [1, 5, 19, 40, 80]) for (const nt of [0, 2, 3]) {
  const w = mkWorld(n, nt), g = w.grid, tag = `n=${n} teams=${nt}`;
  const d = deskSpawn(w), room = d && w.roomAt(d.x, d.z);
  const rooms = w.rooms.filter((r) => { const p = g.findPath(d.x, d.z, r.p.x, r.p.z), l = p[p.length - 1]; return l && Math.hypot(l.x - r.p.x, l.z - r.p.z) < 0.8; });
  check(`spawn ${tag}: cạnh bàn trong phòng điều phối viên, không bị chặn, tới được ${rooms.length}/${w.rooms.length} phòng`,
    d && room && room.key === 'mgr' && !blockedAt(g, d.x, d.z, WALK_R) && nearDesk(w, d) && rooms.length === w.rooms.length);
  // điểm vào chế độ đi bộ: ngay ngoài cửa phòng điều phối viên (khu làm việc), không bị chặn, đi tới được bàn của mình
  const e = doorSpawn(w), mr = w.rooms.find((r) => r.key === 'mgr');
  const toDesk = e && g.findPath(e.x, e.z, d.x, d.z), lastD = toDesk && toDesk[toDesk.length - 1];
  check(`doorSpawn ${tag}: ngoài cửa phòng điều phối viên, không bị chặn, tới được bàn`,
    e && !w.roomAt(e.x, e.z) && !blockedAt(g, e.x, e.z, WALK_R) && Math.hypot(e.x - mr.door.x, e.z - mr.door.z) <= 2.6 &&
    lastD && Math.hypot(lastD.x - d.x, lastD.z - d.z) < 0.8 && Number.isFinite(e.yaw));
  if (n !== 5) continue;
  const scene = new THREE.Scene(), cam = new THREE.PerspectiveCamera(45, 1.6, 0.3, 400);
  const wk = new Walker({ camera: cam, scene });
  wk.setIdentity({ id: 'mgr', name: 'Điều phối viên', color: '#c8413a' });
  wk.enter(w);
  check(`walker ${tag}: vào chế độ đi bộ đứng ở doorSpawn (ngoài cửa phòng, nhìn vào văn phòng)`, wk.isCoordinator && Math.hypot(wk.pos.x - e.x, wk.pos.z - e.z) < 1e-6 && wk.view === e.yaw);
  wk.sit(w.mgrSpot);
  wk.key('KeyW', true, false);
  for (let i = 0; i < 30; i++) { wk.update(0.1, i * 0.1, false); wk.updateCamera(0.1, false); }
  check(`walker ${tag}: ngồi bàn → phím đi không di chuyển, chibi ở ghế`, wk.seated && wk.pos.x === w.mgrSpot.x && wk.pos.z === w.mgrSpot.z && wk.chibi.pose === 'type');
  wk.key('KeyW', false, false);
  // văn phòng dựng lại (thêm agent) khi đang ngồi: bám ghế điều phối viên của World MỚI, không giữ ghế cũ
  const w2 = mkWorld(n + 7, nt);
  wk.setWorld(w2);
  check(`walker ${tag}: dựng lại World khi đang ngồi → ghế là mgrSpot của World mới`, wk.seated === w2.mgrSpot && wk.pos.x === w2.mgrSpot.x && wk.pos.z === w2.mgrSpot.z);
  const w3 = new World({ teams: [], agents: [{ id: 'ag0' }], manager_id: null });
  wk.setWorld(w3);
  check(`walker ${tag}: dựng lại World nhỏ hơn → ghế mới hoặc đứng ở ô trống (không giữ ghế cũ)`,
    wk.seated ? wk.seated === w3.mgrSpot : !blockedAt(w3.grid, wk.pos.x, wk.pos.z, WALK_R));
  wk.setWorld(w); wk.sit(w.mgrSpot);
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

// ---- trả lời qua SSE (steer): khung refresh (run.completed / duyệt / câu hỏi) KHÔNG được xoá câu trả lời cuối
const fn = (k) => typeof T[k] === 'function';
const feed = (mode, frames) => {
  const r = { pending: '', mode, seq: 0 }, out = [];
  for (const f of frames) if (T.replyFrame(r, f) === 'end') { out.push(r.pending); break; }
  return { r, out };
};
if (fn('replyFrame')) {
  const run = [
    { t: 'item', kind: 'prompt', seq: 1 }, { t: 'delta', k: 'text', text: 'Câu trả lời cũ', seq: 2 }, { t: 'item', kind: 'msg', seq: 3 },
    { t: 'item', kind: 'steer', seq: 4 }, { t: 'refresh' }, { t: 'item', kind: 'note', seq: 5 }, { t: 'refresh' },
    { t: 'delta', k: 'text', text: 'Đã xem ', seq: 6 }, { t: 'refresh' }, { t: 'delta', k: 'text', text: 'src/ xong', seq: 7 }, { t: 'item', kind: 'msg', seq: 8 },
    { t: 'item', kind: 'approval', seq: 9 }, { t: 'refresh' }, { t: 'item', kind: 'question', seq: 10 }, { t: 'refresh' },
    { t: 'stats' }, { t: 'item', kind: 'end', seq: 11 }, { t: 'refresh' }, { t: 'end', reason: 'closed' },
  ];
  const a = feed('steer', run);
  check(`steer: run kết thúc (run.completed/duyệt/câu hỏi → refresh) vẫn giữ câu trả lời cuối (${JSON.stringify(a.out[0])})`, a.out[0] === 'Đã xem src/ xong\n');
  check('steer: seq theo khung mới nhất (để nối lại tiếp từ đó)', a.r.seq === 11);
  const b = feed('run', run);
  check('run mới: giữ toàn bộ chữ của agent', b.out[0] === 'Câu trả lời cũ\nĐã xem src/ xong\n');
  const c = { pending: '', mode: 'steer', seq: 0 };
  T.replyFrame(c, { t: 'item', kind: 'msg', html: '<img src=x onerror=alert(1)>' });
  check('khung item: không đọc trường html', c.pending === '');
} else check('talk.js export replyFrame (xử lý khung SSE trả lời)', false);
{
  const src = (await import('node:fs')).readFileSync(new URL(url('office/talk.js')), 'utf8');
  check('talk.js: luồng trả lời dùng replyFrame, không xoá chữ theo khung refresh', src.includes('replyFrame(s, f)') && !/t === 'refresh'/.test(src));
}
check('confirmKey: run_start / run_continue luôn hỏi lại (không cache)', fn('confirmKey') && T.confirmKey(talkPlan({ id: 'a', status: 'idle' }, {}, 'x')) === null
  && T.confirmKey(talkPlan({ id: 'a', status: 'idle' }, { lastRun: 'r0' }, 'x')) === null);
check('confirmKey: steer gắn run_id', fn('confirmKey') && T.confirmKey(talkPlan({ id: 'a', status: 'working', run_id: 'r1' }, {}, 'x')) === 'steer:r1'
  && T.confirmKey(talkPlan({ id: 'a', status: 'working', run_id: 'r2' }, {}, 'x')) === 'steer:r2');
check('retryDelay: lùi dần, có trần, có giới hạn lần', fn('retryDelay') && T.retryDelay(0) === 1000 && T.retryDelay(1) === 2000 && T.retryDelay(5) <= 30000
  && T.retryDelay(T.RETRY_MAX) === null);
check('resetsRetry: chỉ delta/item xoá bộ đếm thử lại, hello/refresh/end thì không', fn('resetsRetry')
  && T.resetsRetry({ t: 'delta', k: 'text', text: 'a' }) && T.resetsRetry({ t: 'item', kind: 'msg' })
  && !T.resetsRetry({ t: 'hello' }) && !T.resetsRetry({ t: 'refresh' }) && !T.resetsRetry({ t: 'end' }));
check('streamUrl: lần đầu không since; nối lại kèm since + epoch', fn('streamUrl') && T.streamUrl('r 1', { seq: 0 }) === '/sse/run/r%201'
  && T.streamUrl('r1', { seq: 42, epoch: 'e1' }) === '/sse/run/r1?since=42&epoch=e1' && T.streamUrl('r1', { seq: 42 }) === '/sse/run/r1?since=42');
if (fn('replyFrame')) {
  // daemon khởi động lại: hello epoch mới → phát lại cả run từ đầu; đoạn dở cũ phải bỏ, không nhân đôi
  const r = { pending: '', mode: 'run', seq: 0 };
  for (const f of [{ t: 'hello', epoch: 'e1' }, { t: 'delta', k: 'text', text: 'Xin ', seq: 5 }, { t: 'delta', k: 'text', text: 'chào', seq: 6 }]) T.replyFrame(r, f);
  const same = { ...r }; T.replyFrame(same, { t: 'hello', epoch: 'e1' });
  T.replyFrame(r, { t: 'hello', epoch: 'e2' });
  for (const f of [{ t: 'delta', k: 'text', text: 'Xin ', seq: 1 }, { t: 'delta', k: 'text', text: 'chào', seq: 2 }]) T.replyFrame(r, f);
  check('replyFrame: epoch đổi → bỏ đoạn dở, phát lại không nhân đôi', r.pending === 'Xin chào' && r.seq === 2 && r.epoch === 'e2');
  check('replyFrame: hello cùng epoch → giữ nguyên đoạn dở và seq', same.pending === 'Xin chào' && same.seq === 6);
}
check('TALK_ORDER dưới nền mờ (1000) / bảng modal (1001)', typeof T.TALK_ORDER === 'number' && T.TALK_ORDER < 1000);
process.exit(fails ? 1 : 0);
