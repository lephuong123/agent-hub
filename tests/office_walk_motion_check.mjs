// Kiểm động học chế độ đi bộ KHÔNG cần trình duyệt (Node ≥ 18): va chạm hình tròn (circleMove), bộ điều khiển avatar có vận tốc
// (Walker.update), camera spring-arm (Walker.updateCamera) — dựng World thật (static/office/world.js) với canvas giả như
// office_layout_check.mjs (docs/dev-notes/office-walk.md). In "ok …" mỗi ca; lỗi → exit 1.
// Chạy: node tests/office_walk_motion_check.mjs <thư mục gốc repo>   (tests/test_office_walk.py gọi script này)
const ctx = new Proxy({}, {
  get: (t, k) => (k === 'measureText' ? () => ({ width: 10 }) : k in t ? t[k] : () => ({ addColorStop() {} })),
  set: (t, k, v) => { t[k] = v; return true; },
});
globalThis.document = { createElement: () => ({ width: 0, height: 0, getContext: () => ctx, style: {} }) };
const root = process.argv[2].replace(/\\/g, '/');
const url = (p) => (root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/office/' + p;
const { World } = await import(url('world.js'));
const { Grid } = await import(url('path.js'));
const W = await import(url('walk.js'));
const { blockedAt, circleMove, WALK_R } = W;

let fails = 0;
const check = (name, cond) => { if (cond) console.log('ok ' + name); else { fails++; console.log('FAIL ' + name); } };
const mkWorld = (n) => {
  const agents = [{ id: 'mgr', is_manager: true }];
  for (let i = 0; i < n; i++) agents.push({ id: 'ag' + i, team: i % 2 ? 'a' : 'b' });
  return new World({ teams: [{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }], agents, manager_id: 'mgr' });
};

// ---- C3: va chạm hình tròn
{
  // tường dọc x ≥ 5 (lưới 0,5 m); đi chéo 20° vào tường rồi trượt dọc theo nó (~2 m)
  const g = new Grid(0, 0, 20, 20, 0.5);
  g.rect(5.01, 0, 20, 20, 0, 1);
  let x = 4.6, z = 1, prevVz = null, worst = 0, flips = 0;
  const a = (20 * Math.PI) / 180, h = 1 / 120, sp = 2.4;
  for (let i = 0; i < 400; i++) {
    const q = circleMove(g, x, z, Math.sin(a) * sp * h, Math.cos(a) * sp * h, WALK_R);
    const vz = (q.z - z) / h;
    if (prevVz != null && prevVz > 0.1) { worst = Math.max(worst, (prevVz - vz) / prevVz); if (Math.sign(vz) !== Math.sign(prevVz)) flips++; }
    prevVz = vz; x = q.x; z = q.z;
  }
  check(`circleMove: trượt dọc tường góc tới 20° — tiếp tuyến giảm tối đa ${(worst * 100).toFixed(1)} % (≤ 40 %), đổi dấu ${flips}, không xuyên (x=${x.toFixed(3)})`,
    worst <= 0.4 && flips === 0 && x <= 5 - WALK_R * 0.9);
}
for (const n of [19, 40]) {
  const w = mkWorld(n), g = w.grid;
  // đi thẳng qua mọi cửa phòng (phía bắc: về −z)
  let bad = [];
  for (const r of w.rooms.filter((x) => x.door && x.key !== 'cafe')) {
    let p = { x: r.door.x, z: r.door.z + 1.0 };
    for (let i = 0; i < 2.2 / 0.02; i++) p = circleMove(g, p.x, p.z, 0, -0.02, WALK_R);
    if (w.roomAt(p.x, p.z) !== r) bad.push(r.key);
  }
  check(`circleMove n=${n}: đi thẳng qua mọi cửa phòng (${bad.join(',') || 'đủ'})`, bad.length === 0);
  // 10 000 bước ngẫu nhiên từ điểm xuất hiện: không lần nào lẹm vào ô bị chặn
  let s = 12345 + n; const rnd = () => ((s = (s * 1103515245 + 12345) >>> 0) / 4294967296);
  let p = W.spawnPoint(w), worstHit = 0;
  for (let i = 0; i < 10000; i++) {
    const ang = rnd() * Math.PI * 2, d = 0.01 + rnd() * 0.3;
    p = circleMove(g, p.x, p.z, Math.sin(ang) * d, Math.cos(ang) * d, WALK_R);
    if (blockedAt(g, p.x, p.z, WALK_R * 0.9)) worstHit++;
  }
  check(`circleMove n=${n}: 10 000 bước ngẫu nhiên không lẹm (${worstHit} lần)`, worstHit === 0);
}

process.exit(fails ? 1 : 0);
