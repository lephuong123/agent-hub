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
const { blockedAt, circleMove, WALK_R, Walker } = W;
const THREE = await import((root.startsWith('/') ? 'file://' : 'file:///') + root + '/src/cfharness/web/static/vendor/three-0.169.0/three.module.min.js');

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

// ---- C4: bộ điều khiển avatar có vận tốc
const DT = { đều: () => 1 / 60, 'xen kẽ': (i) => (i % 2 ? 1 / 30 : 1 / 60), 'gai 0,1': (i) => (i === 50 ? 0.1 : 1 / 60) };
const mkWalker = (w) => {
  const cam = new THREE.PerspectiveCamera(36, 16 / 9, 0.3, 400), scene = new THREE.Scene();
  const wk = new Walker({ camera: cam, scene });
  wk.enter(w);
  // hướng nhìn có ≥ 4 m trống trước mặt (đi thẳng không đụng vật cản — đo động học, không đo va chạm)
  for (let k = 0; k < 72; k++) {
    const a = wk.view + (k % 2 ? 1 : -1) * Math.ceil(k / 2) * (Math.PI / 36);
    const q = circleMove(w.grid, wk.pos.x, wk.pos.z, Math.sin(a) * 4, Math.cos(a) * 4, WALK_R * 1.5);
    if (Math.hypot(q.x - wk.pos.x - Math.sin(a) * 4, q.z - wk.pos.z - Math.cos(a) * 4) < 1e-3) { wk.view = wk.yaw = a; break; }
  }
  return { wk, cam, scene };
};
{
  const w = mkWorld(19);
  for (const [name, f] of Object.entries(DT)) {
    const { wk } = mkWalker(w);
    // đặt avatar ở chỗ thoáng, nhìn về hướng có ≥ 2 m trống
    let t = 0, i = 0, amax = 0, prev = { x: 0, z: 0 }, stopT = null, settle = null;
    while (t < 3) {
      const dt = f(i++), on = (t % 1.2) < 0.6;
      if (on) wk.keys.add('KeyW'); else wk.keys.delete('KeyW');
      wk.update(dt, t, false); t += dt;
      const a = Math.hypot(wk.vel.x - prev.x, wk.vel.z - prev.z) / dt;
      if (dt < 0.05) amax = Math.max(amax, a); // khung gai 0,1 s: Δv/dt trung bình trên cả khung
      prev = { x: wk.vel.x, z: wk.vel.z };
      if (!on && stopT == null && t > 0.6) stopT = t - dt;
      if (stopT != null && settle == null && Math.hypot(wk.vel.x, wk.vel.z) < 0.05) settle = t - stopT;
    }
    check(`avatar (${name}): gia tốc tối đa ${amax.toFixed(1)} m/s² (≤ 40), dừng hẳn ${(settle * 1000).toFixed(0)} ms sau khi nhả (≤ 250)`,
      amax <= 40 && settle != null && settle <= 0.25);
  }
  // tự đi tới đích: hãm dần, không lẹm; reduced → dịch chuyển tức thời
  const { wk } = mkWalker(w);
  const goal = w.rooms.find((r) => r.key === 'pantry').p;
  wk.goTo(goal.x, goal.z, false);
  let t = 0, clip = 0, endSpeed = 0, steps = 0;
  const end = wk.path.length ? wk.path[wk.path.length - 1] : goal;
  while (wk.path.length && t < 30) {
    wk.update(1 / 60, t, false); t += 1 / 60; steps++;
    if (blockedAt(w.grid, wk.pos.x, wk.pos.z, WALK_R * 0.9)) clip++;
    if (Math.hypot(end.x - wk.pos.x, end.z - wk.pos.z) < 0.1) endSpeed = Math.max(endSpeed, Math.hypot(wk.vel.x, wk.vel.z));
  }
  const miss = Math.hypot(end.x - wk.pos.x, end.z - wk.pos.z);
  check(`tự đi: tới đích sau ${t.toFixed(1)} s (lệch ${miss.toFixed(2)} m), 0,1 m cuối ≤ ${endSpeed.toFixed(2)} m/s (< 0,6), lẹm ${clip}`,
    !wk.path.length && miss < 0.1 && endSpeed < 0.6 && clip === 0);
  const r2 = mkWalker(w).wk, lib = w.rooms.find((r) => r.key === 'library').p;
  r2.goTo(lib.x, lib.z, true);
  check(`reduced-motion: bấm sàn → dịch chuyển tức thời (lệch ${Math.hypot(r2.pos.x - lib.x, r2.pos.z - lib.z).toFixed(2)} m, không còn đường)`,
    !r2.path.length && Math.hypot(r2.pos.x - lib.x, r2.pos.z - lib.z) < 0.8);
}

// ---- C5: camera spring-arm
{
  const w = mkWorld(19);
  // chỗ đứng thoáng: trước mặt ≥ 4 m và sau lưng (hướng cần camera) ≥ 4 m không có vật cao hơn tia
  const pick = () => {
    const m = mkWalker(w), wk = m.wk;
    for (let k = 0; k < 72; k++) {
      const a = wk.view + (k % 2 ? 1 : -1) * Math.ceil(k / 2) * (Math.PI / 36);
      const cp = Math.cos(-0.12 - 0.35), dy = Math.sin(0.35 + 0.12);
      const q = circleMove(w.grid, wk.pos.x, wk.pos.z, Math.sin(a) * 4, Math.cos(a) * 4, WALK_R * 1.5);
      const free = Math.hypot(q.x - wk.pos.x - Math.sin(a) * 4, q.z - wk.pos.z - Math.cos(a) * 4) < 1e-3;
      const back = W.armHit(w.grid, wk.pos.x, 1, wk.pos.z, -Math.sin(a) * cp, dy, -Math.cos(a) * cp, 4.5) > 4.5;
      if (free && back) { wk.view = wk.yaw = a; return m; }
    }
    return m;
  };
  const dist = (wk, cam) => Math.hypot(cam.position.x - wk.pos.x, cam.position.y - 1, cam.position.z - wk.pos.z);
  for (const [name, f] of Object.entries({ đều: () => 1 / 60, 'xen kẽ': (i) => (i % 2 ? 1 / 30 : 1 / 60) })) {
    const { wk, cam } = pick();
    wk.updateCamera(1 / 60, true); // reduced: gán thẳng (camera đã ở sau lưng)
    let t = 0, i = 0, dmin = 9, dmax = 0, prev = null; const sp = [];
    wk.keys.add('KeyW');
    while (t < 1.6) {
      const dt = f(i++); wk.update(dt, t, false); wk.updateCamera(dt, false); t += dt;
      if (t > 0.5) {
        const d = dist(wk, cam); dmin = Math.min(dmin, d); dmax = Math.max(dmax, d);
        if (prev) sp.push(Math.hypot(cam.position.x - prev.x, cam.position.z - prev.z) / dt);
      }
      prev = cam.position.clone();
    }
    const m = sp.reduce((a, b) => a + b, 0) / sp.length, cv = Math.sqrt(sp.reduce((a, b) => a + (b - m) ** 2, 0) / sp.length) / m;
    check(`camera đi thẳng (dt ${name}): khoảng cách ${dmin.toFixed(3)}–${dmax.toFixed(3)} ∈ [3,35; 3,45], CV tốc độ ${cv.toFixed(4)} (≤ 0,01)`,
      dmin >= 3.35 && dmax <= 3.45 && cv <= 0.01);
  }
  {
    // chỗ thoáng mọi hướng (cần không chạm gì khi xoay 360°)
    const { wk, cam } = mkWalker(w), g = w.grid, cp = Math.cos(-0.47), dy = Math.sin(0.47);
    let spot = null;
    for (let r = 0; r < g.rows && !spot; r++) for (let c = 0; c < g.cols && !spot; c++) {
      const [x, z] = g.center(c, r);
      if (blockedAt(g, x, z, WALK_R)) continue;
      let ok = true;
      for (let k = 0; k < 36 && ok; k++) { const a = (k * Math.PI) / 18; ok = W.armHit(g, x, 1, z, -Math.sin(a) * cp, dy, -Math.cos(a) * cp, 3.6) > 3.6; }
      if (ok) spot = { x, z };
    }
    wk.pos = spot; wk.rigInit = false;
    wk.updateCamera(1 / 60, true);
    let dmin = 9, dmax = 0;
    for (let i = 0; i < 30; i++) { wk.look(-6 / 0.006 / 60, 0); wk.update(1 / 60, i / 60, false); wk.updateCamera(1 / 60, false); const d = dist(wk, cam); dmin = Math.min(dmin, d); dmax = Math.max(dmax, d); }
    check(`camera xoay 6 rad/s (chỗ thoáng): khoảng cách ${dmin.toFixed(3)}–${dmax.toFixed(3)} ∈ [3,35; 3,45]`, !!spot && dmin >= 3.35 && dmax <= 3.45);
  }
  // đứng sát tủ cao / tường, xoay 360°: camera không nằm trong ô bị chặn có vật cao hơn camera
  {
    const { wk, cam } = mkWalker(w);
    const g = w.grid; let bad = 0, n = 0;
    const tall = [];
    for (let r = 0; r < g.rows; r++) for (let c = 0; c < g.cols; c++) if (g.blocked(c, r) && g.top[r * g.cols + c] >= 1.3 && g.top[r * g.cols + c] < 2.55) tall.push([c, r]);
    for (const [c, r] of tall.slice(0, 40)) {
      const [x, z] = g.center(c, r), fr = g.nearestFree(c, r), [fx, fz] = g.center(fr[0], fr[1]);
      if (blockedAt(g, fx, fz, WALK_R)) continue;
      wk.pos = { x: fx, z: fz }; wk.rigInit = false; wk.updateCamera(1 / 60, true);
      for (let k = 0; k < 36; k++) {
        wk.view += Math.PI / 18; wk.updateCamera(1 / 60, true); n++;
        const [cc, rr] = g.cellOf(cam.position.x, cam.position.z);
        // đồ đạc (không tính ô tường 2,6: ô lưới 0,5 m dày hơn tường thật, camera sát tường vẫn được làm mờ như cũ)
        const top = g.inside(cc, rr) && g.blocked(cc, rr) ? g.top[rr * g.cols + cc] : 0;
        if (top < 2.55 && top > cam.position.y) bad++;
      }
    }
    check(`camera sát đồ cao (${tall.length} ô ≥ 1,3 m, ${n} góc): không chui vào đồ đạc (${bad})`, n > 0 && bad === 0);
  }
  // không cấp phát Set mỗi khung (đếm qua spy)
  {
    const { wk } = pick();
    const S0 = globalThis.Set; let made = 0;
    globalThis.Set = class extends S0 { constructor(a) { super(a); made++; } };
    wk.keys.add('KeyW');
    for (let i = 0; i < 120; i++) { wk.update(1 / 60, i / 60, false); wk.updateCamera(1 / 60, false); }
    globalThis.Set = S0;
    check(`120 khung đi bộ: không tạo Set mới (${made})`, made === 0);
  }
}

process.exit(fails ? 1 : 0);
