// Kiểm bố cục văn phòng 3D KHÔNG cần trình duyệt (Node ≥ 18): dựng World thật (static/office/world.js) với canvas giả, rồi kiểm
// số chỗ, chồng lấn, A* tới được (bàn ↔ cafe ↔ pantry/quầy/cửa ra) và chế độ đi bộ: avatar từ cửa vào tới được MỌI phòng
// (bấm sàn = đi theo A*), đi thẳng bằng phím qua cửa phòng được (va chạm của walk.js), không xuyên được tường/vách. In một dòng mỗi ca; lỗi → exit 1.
// Chạy: node tests/office_layout_check.mjs <thư mục gốc repo>   (tests/test_office_walk.py gọi script này)
const ctx = new Proxy({}, {
  get: (t, k) => (k === 'measureText' ? () => ({ width: 10 }) : k in t ? t[k] : () => ({ addColorStop() {} })),
  set: (t, k, v) => { t[k] = v; return true; },
});
globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => ctx, style: {} }) };
const root = process.argv[2].replace(/\\/g, '/');
const url = (p) => (root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/office/' + p;
const { World } = await import(url('world.js'));
const { blockedAt, slideMove, spawnPoint, WALK_R } = await import(url('walk.js'));

let fails = 0;
const bad = (m) => { fails++; console.log('FAIL ' + m); };
const reach = (g, a, b) => {
  const p = g.findPath(a.x, a.z, b.x, b.z);
  const last = p[p.length - 1];
  if (!last) return false;
  // mọi chặng (trừ chặng cuối vào ghế/sofa) phải đi được thật
  let x = a.x, z = a.z;
  for (let i = 0; i < p.length - 1; i++) { if (!g.lineFree(x, z, p[i].x, p[i].z)) return false; x = p[i].x; z = p[i].z; }
  // chặng cuối xuyên đồ đạc chỉ chấp nhận khi ngắn (vào ghế); dài → A* không tới nơi, findPath "nhảy" xuyên tường
  return g.lineFree(x, z, b.x, b.z) || Math.hypot(x - b.x, z - b.z) < 1.5;
};
for (const n of [1, 5, 19, 40, 80]) for (const withTeams of [false, true]) {
  const teams = withTeams ? [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }] : [];
  const agents = [{ id: 'mgr', is_manager: true }];
  for (let i = 0; i < n; i++) agents.push({ id: 'ag' + i, team: withTeams ? (i % 2 ? 'a' : 'b') : null });
  const w = new World({ teams, agents, manager_id: 'mgr' }), g = w.grid, tag = `n=${n} teams=${withTeams}`;
  // số chỗ cafe + giữ chỗ
  if (w.zones.cafe.length < n) bad(`${tag}: cafe ${w.zones.cafe.length} < ${n}`);
  for (let i = 0; i < n; i++) if (!w.reserve('cafe', 'ag' + i, null)) bad(`${tag}: không giữ được chỗ cafe cho ag${i}`);
  // không chồng lấn: cụm bàn < cafe < dải nam
  const maxZ = Math.max(...w.clusters.map((c) => c.z + c.d));
  if (!(maxZ < w.cafe.z0 && w.zs >= w.cafe.z1)) bad(`${tag}: chồng lấn cụm bàn/cafe/dải nam`);
  // agent: bàn → cafe → pantry/quầy, bàn → cửa ra
  const entrance = { x: w.W - 1.4, z: w.D - 0.6 };
  const seat0 = w.seats.get('ag0');
  for (const s of [...w.zones.cafe.slice(0, 6), ...w.zones.pantry, ...w.zones.cafeBar, ...w.zones.bench]) {
    if (!reach(g, seat0, s.stand || s)) bad(`${tag}: bàn ag0 → ${s.zone} (${s.x.toFixed(1)},${s.z.toFixed(1)})`);
  }
  if (!reach(g, seat0, entrance)) bad(`${tag}: bàn → cửa ra`);
  // chế độ đi bộ: điểm vào của avatar không bị chặn; tới được mọi phòng (A* + đi có va chạm)
  // điểm xuất hiện: giữa khu làm việc (ngoài mọi phòng), xa cửa ra (≥ 4 m: không vừa vào đã thấy "[E] Thoát" / camera dí vào cửa)
  const start = spawnPoint(w);
  if (blockedAt(g, start.x, start.z, WALK_R)) bad(`${tag}: điểm vào của avatar bị chặn`);
  if (w.roomAt(start.x, start.z)) bad(`${tag}: điểm vào nằm trong phòng ${w.roomAt(start.x, start.z).key}`);
  if (Math.hypot(w.W - 1.4 - start.x, w.D - start.z) < 4) bad(`${tag}: điểm vào sát cửa ra (${start.x.toFixed(1)},${start.z.toFixed(1)})`);
  if (Math.abs(start.x - w.W / 2) > w.W * 0.25) bad(`${tag}: điểm vào không ở giữa văn phòng (x=${start.x.toFixed(1)}, W=${w.W})`);
  const keys = w.rooms.map((r) => r.key).sort().join(',');
  if (keys !== 'bench,cafe,library,lounge,meeting,mgr,pantry,server') bad(`${tag}: danh sách phòng ${keys}`);
  for (const r of w.rooms) {
    if (blockedAt(g, r.p.x, r.p.z, WALK_R)) { bad(`${tag}: điểm trong ${r.key} bị chặn`); continue; }
    if (!reach(g, start, r.p)) bad(`${tag}: avatar không tới được ${r.key}`);
    if (w.roomAt(r.p.x, r.p.z) !== r) bad(`${tag}: điểm trong ${r.key} nằm ngoài phòng`);
  }
  // đi thẳng (phím W) xuyên cửa phòng phía bắc: từ trước cửa đi về -z → vào trong phòng
  for (const r of w.rooms.filter((x) => x.door && x.key !== 'cafe')) {
    const p = slideMove(g, r.door.x, r.door.z + 1.0, 0, -2.2, WALK_R);
    if (w.roomAt(p.x, p.z) !== r || p.z > r.door.z - 0.8) bad(`${tag}: đi thẳng qua cửa ${r.key} bị kẹt ở z=${p.z.toFixed(2)}`);
  }
  // không xuyên tường: đi ngang qua vách giữa server|thư viện, và đi về phía bắc từ khu làm việc chỗ không có cửa
  const ww = slideMove(g, w.rw - 1.0, 3.0, 2.0, 0, WALK_R);
  if (ww.x > w.rw) bad(`${tag}: xuyên vách server → thư viện (x=${ww.x.toFixed(2)})`);
  const wn = slideMove(g, w.rw * 0.15, 6.9, 0, -2.0, WALK_R);
  if (wn.z < 6.0) bad(`${tag}: xuyên vách trước phòng server (z=${wn.z.toFixed(2)})`);
  const wo = slideMove(g, 3, 3, 0, -6, WALK_R);
  if (wo.z < 0) bad(`${tag}: xuyên tường bắc`);
  if (fails === 0) console.log(`ok ${tag}: W=${w.W} D=${w.D.toFixed(1)} cafe=${w.zones.cafe.length} rooms=${w.rooms.length}`);
}
process.exit(fails ? 1 : 0);
